import com.google.gson.*;
import com.microsoft.java.debug.core.DebugSettings;
import com.microsoft.java.debug.core.JavaBreakpointLocation;
import com.microsoft.java.debug.core.adapter.*;
import com.microsoft.java.debug.core.protocol.JsonUtils;
import com.microsoft.java.debug.core.protocol.Messages;
import com.microsoft.java.debug.core.adapter.variables.VariableProxy;
import com.microsoft.java.debug.core.protocol.Types;
import com.microsoft.java.debug.core.adapter.variables.StackFrameReference;
import com.sun.jdi.AbsentInformationException;
import com.sun.jdi.ArrayReference;
import com.sun.jdi.ArrayType;
import com.sun.jdi.ClassLoaderReference;
import com.sun.jdi.ClassObjectReference;
import com.sun.jdi.LocalVariable;
import com.sun.jdi.ObjectReference;
import com.sun.jdi.ReferenceType;
import com.sun.jdi.StackFrame;
import java.lang.reflect.Proxy;
import java.net.URI;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/** Experimental Microsoft-core launcher. Only explicit, prepared source maps are accepted. */
public final class PeekyJavaAdapter extends ProtocolServer {
    private static final Set<String> COMMANDS = Set.of("initialize", "launch", "attach", "setBreakpoints",
        "configurationDone", "threads", "stackTrace", "scopes", "variables", "setVariable",
        "continue", "next", "stepIn", "stepOut", "disconnect", "peekyArrayHolder");
    private static final Set<String> STEPS = Set.of("next", "stepIn", "stepOut");
    /** The JDK's own class loaders besides the bootstrap one. */
    private static final Set<String> JDK_LOADERS = Set.of(
        "jdk.internal.loader.ClassLoaders$PlatformClassLoader", "sun.misc.Launcher$ExtClassLoader");
    /** At most this many objects referring to an array are read before writing it. */
    private static final int MAX_REFERRERS = 64;
    /** And through at most this many arrays holding it. */
    private static final int MAX_DEPTH = 4;
    /** The longest wait for the JVM's search for what refers to an array, and the least. */
    private static final long SEARCH_MS = 2000, MIN_SEARCH_MS = 250;
    /** The same search's longest wait, and least, when a listing asks it. */
    private static final long LIST_SEARCH_MS = 500, MIN_LIST_SEARCH_MS = 50;
    /** What a write keeps of the session's pause budget for its other requests. */
    private static final long PAUSE_MARGIN_MS = 1000;
    /**
     * The JDK fields whose array holds the program's own values as its elements: the
     * backing arrays of ArrayList, Vector and ArrayDeque, and of Arrays.asList's list (the
     * caller's own array) and its iterator. No JDK state depends on what those elements hold.
     */
    private static final Set<String> USER_ELEMENTS = Set.of("java.util.ArrayList.elementData",
        "java.util.Vector.elementData", "java.util.ArrayDeque.elements", "java.util.Arrays$ArrayList.a",
        "java.util.Arrays$ArrayItr.a");
    /** One search at a time, off the request thread, so its wait can be bounded. */
    private static final ExecutorService SEARCH = Executors.newSingleThreadExecutor(task -> {
        Thread thread=new Thread(task, "Peeky referrer search");
        thread.setDaemon(true);
        return thread;
    });

    /** Fixed step filters: steps never stop inside the JDK; requests cannot change them. */
    private static JsonObject stepFilters() {
        JsonObject filters=new JsonObject();
        JsonArray skip=new JsonArray();
        for (String pattern : List.of("java.*", "javax.*", "jdk.*", "sun.*", "com.sun.*")) skip.add(pattern);
        filters.add("skipClasses", skip);
        // Lambda bodies are synthetic methods: skipping them would make them unreachable.
        filters.addProperty("skipSynthetics", false);
        filters.addProperty("skipStaticInitializers", false);
        filters.addProperty("skipConstructors", false);
        return filters;
    }
    private final SourceMap sources;
    /** The container each pending `variables` request lists, by request seq. */
    private final Map<Integer, Integer> listings = new ConcurrentHashMap<>();
    /** The JVM a pending change suspended, by request seq: resumed before its reply. */
    private final Map<Integer, com.sun.jdi.VirtualMachine> paused = new ConcurrentHashMap<>();

    private PeekyJavaAdapter(ProviderContext context, SourceMap sources) {
        super(System.in, System.out, context);
        this.sources = sources;
    }

    @Override protected void dispatchRequest(Messages.Request request) {
        try {
            if (!COMMANDS.contains(request.command)) throw new IllegalArgumentException("unsupported command");
            JsonObject args = request.arguments;
            if (request.command.equals("launch")) {
                if (!sources.classes.containsKey(args.get("mainClass").getAsString()))
                    throw new IllegalArgumentException("mainClass absent from prepared source map");
                if (args.has("noDebug") && args.get("noDebug").getAsBoolean())
                    throw new IllegalArgumentException("noDebug unsupported");
                if (args.has("console") && !args.get("console").getAsString().equals("internalConsole"))
                    throw new IllegalArgumentException("only internalConsole supported");
                args.addProperty("console", "internalConsole");
                args.add("stepFilters", stepFilters());
            }
            if (request.command.equals("attach")) {
                // Loopback JDWP only; remote JVMs must be reached through an SSH tunnel.
                if (args==null || !args.has("hostName") || !args.has("port")
                    || !args.get("hostName").isJsonPrimitive() || !args.get("port").isJsonPrimitive())
                    throw new IllegalArgumentException("attach requires loopback hostName and port");
                String host=args.get("hostName").getAsString();
                int port=args.get("port").getAsInt();
                if (!host.equals("127.0.0.1") && !host.equals("localhost"))
                    throw new IllegalArgumentException("attach is loopback-only");
                if (port<1 || port>65535) throw new IllegalArgumentException("invalid attach port");
                for (String key : new ArrayList<>(args.keySet()))
                    if (!Set.of("hostName", "port", "timeout").contains(key)) args.remove(key);
                args.add("stepFilters", stepFilters());
            }
            if (STEPS.contains(request.command)) {
                // Only the thread: no step-in targets, granularity or other options.
                JsonElement id=args==null ? null : args.get("threadId");
                if (id==null || !id.isJsonPrimitive() || !id.getAsJsonPrimitive().isNumber())
                    throw new IllegalArgumentException("step requires a numeric threadId");
                long thread;
                try {
                    thread=id.getAsBigDecimal().longValueExact();
                } catch (ArithmeticException error) {
                    throw new IllegalArgumentException("step threadId must be a positive integer");
                }
                if (thread<1) throw new IllegalArgumentException("step threadId must be a positive integer");
                for (String key : new ArrayList<>(args.keySet())) args.remove(key);
                args.addProperty("threadId", thread);
            }
            if (request.command.equals("setBreakpoints")) {
                if (args.has("breakpoints")) for (JsonElement e : args.getAsJsonArray("breakpoints")) {
                    JsonObject bp=e.getAsJsonObject();
                    for (String field : List.of("condition", "hitCondition", "logMessage"))
                        if (bp.has(field)) throw new IllegalArgumentException(field + " unsupported");
                }
            }
            if (request.command.equals("setVariable")) {
                String value=args.get("value").getAsString();
                // A text between plain quotes: java-debug takes them off and makes
                // a new String of the rest, for a String variable only (checked below).
                boolean text=value.length()>=2 && value.startsWith("\"") && value.endsWith("\"");
                // One UTF-16 unit between single quotes: java-debug sets a char to
                // it, for a char variable only (checked below).
                boolean character=value.length()==3 && value.startsWith("'") && value.endsWith("'");
                if (!text && !character && !value.matches("(?:true|false|[+-]?[0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"))
                    throw new IllegalArgumentException("only numeric or boolean scalar literals, a quoted char for a char, or text for a String, supported");
                // What is left of the session's pause budget (Peeky's worker), never
                // passed on to java-debug.
                long pauseLeftMs=-1;
                if (args.has("peekyPauseLeftMs")) {
                    pauseLeftMs=Math.max(0, args.get("peekyPauseLeftMs").getAsLong());
                    args.remove("peekyPauseLeftMs");
                }
                // A line breakpoint stops only its own thread: another could make a JDK
                // class hold the array between the check and the write. Every thread stays
                // suspended until the write is made: until its reply (sendResponse), which
                // super.dispatchRequest waits for.
                com.sun.jdi.VirtualMachine vm=suspendForArray(args.get("variablesReference").getAsInt());
                if (vm!=null) paused.put(request.seq, vm);
                checkFieldTarget(args.get("variablesReference").getAsInt(), args.get("name").getAsString());
                if (text) checkDeclaredTarget(args.get("variablesReference").getAsInt(), args.get("name").getAsString(), "java.lang.String", "text changes only a String");
                if (character) checkDeclaredTarget(args.get("variablesReference").getAsInt(), args.get("name").getAsString(), "char", "a quoted char changes only a char");
                checkArrayTarget(args.get("variablesReference").getAsInt(), pauseLeftMs);
            }
            if (request.command.equals("variables") && args!=null && args.has("variablesReference")
                && args.get("variablesReference").isJsonPrimitive())
                listings.put(request.seq, args.get("variablesReference").getAsInt());
            if (request.command.equals("peekyArrayHolder")) {
                // Peeky's own request, never passed on to java-debug.
                Messages.Response response=new Messages.Response(request.seq, request.command, true);
                response.body=arrayHolder(args);
                sendResponse(response);
                return;
            }
            super.dispatchRequest(request);
        } catch (Exception error) {
            sendResponse(new Messages.Response(request.seq, request.command, false,
                "Peeky experimental Java adapter: " + error.getMessage()));
        } finally {
            // Never left suspended, should no reply have been sent.
            com.sun.jdi.VirtualMachine left=paused.remove(request.seq);
            if (left!=null) resumeOrEnd(left);
        }
    }

    /**
     * Resume a JVM a change suspended, or end the launcher where that fails: a JVM
     * whose every thread stays suspended must never outlive the session silently. Ending
     * closes the debugger's connection, which resumes them in an attached JVM too, and
     * Peeky's worker closes the session with the reason the launcher gave on stderr.
     */
    private static void resumeOrEnd(com.sun.jdi.VirtualMachine vm) {
        if (!resume(vm, System.err)) Runtime.getRuntime().halt(3);
    }

    /**
     * Resume ``vm``: true where it runs again, or is gone (VMDisconnectedException: nothing
     * is left to resume). Any other failure would leave every thread suspended: disconnect
     * from it (JDWP's Dispose resumes every thread the debugger suspended), say so on
     * ``err`` in a line that ends Peeky's session (JavaSession.fatal_stderr), and return
     * false.
     */
    static boolean resume(com.sun.jdi.VirtualMachine vm, java.io.PrintStream err) {
        try {
            vm.resume();
            return true;
        } catch (com.sun.jdi.VMDisconnectedException gone) {
            return true;
        } catch (RuntimeException error) {
            String after;
            try {
                vm.dispose();
                after="disconnected from it, which resumes every thread the debugger suspended";
            } catch (RuntimeException also) {
                after="disconnecting failed too (" + also + "); ending the launcher closes the connection";
            }
            err.println("Peeky experimental Java adapter: could not resume the JVM after a change ("
                + error + "); " + after);
            error.printStackTrace(err);
            err.flush();
            return false;
        }
    }

    /**
     * The JVM, suspended (every thread, as counted: a thread stopped before stays so),
     * where ``reference`` is an array whose element a change names; else null.
     */
    private com.sun.jdi.VirtualMachine suspendForArray(int reference) throws ReflectiveOperationException {
        Object container=debugContext().getRecyclableIdPool().getObjectById(reference);
        Object value=container instanceof VariableProxy ? ((VariableProxy) container).getProxiedVariable() : null;
        if (!(value instanceof ArrayReference)) return null;
        com.sun.jdi.VirtualMachine vm=((ArrayReference) value).virtualMachine();
        vm.suspend();
        return vm;
    }

    /**
     * java-debug writes an object's field through fieldByName, which follows Java's hiding
     * rules, while its listing shows instance fields only: a static field of a subclass
     * that hides an inherited instance field would be written in place of the listed one.
     * Allow a field write only when the name means exactly one instance field.
     */
    private void checkFieldTarget(int reference, String name) throws ReflectiveOperationException {
        // `x (Base)` names a field by its class; java-debug resolves those to statics only.
        if (name.contains("(")) throw new IllegalArgumentException("qualified field names unsupported");
        Object container=debugContext().getRecyclableIdPool().getObjectById(reference);
        if (!(container instanceof VariableProxy)) return;
        Object value=((VariableProxy) container).getProxiedVariable();
        if (!(value instanceof ObjectReference) || value instanceof ArrayReference) return;
        ReferenceType type=((ObjectReference) value).referenceType();
        com.sun.jdi.Field visible=type.fieldByName(name);
        long instances=type.allFields().stream().filter(f -> f.name().equals(name) && !f.isStatic()).count();
        if (visible==null || visible.isStatic() || instances!=1)
            throw new IllegalArgumentException("field " + name + " is hidden by, or shares its name with, "
                + "another field of " + type.name() + "; it cannot be changed");
        // A JDK class's own state: a String's cached hash, an ArrayList's size.
        if (jdkLoaded(visible.declaringType()))
            throw new IllegalArgumentException("field " + name + " is declared by "
                + visible.declaringType().name() + ", a JDK class; Peeky never changes the JDK's own fields");
    }

    /**
     * Whether a class is the JDK's own: one in a named module of the JDK's
     * (`java.base`, and `jdk.jshell` or `jdk.compiler`, which the application class loader
     * loads), or, before modules (Java 8) or besides them, one the bootstrap or platform
     * class loader loaded. Never judged by its package's name.
     */
    private static boolean jdkLoaded(ReferenceType type) {
        if (type.virtualMachine().canGetModuleInfo()) {
            String module=type.module().name();
            if (module!=null && (module.startsWith("java.") || module.startsWith("jdk."))) return true;
        }
        ClassLoaderReference loader=type.classLoader();
        return loader==null || JDK_LOADERS.contains(loader.referenceType().name());
    }

    /**
     * Refuse writing an element of an array a JDK class's field holds, by the array's
     * identity: a reflected or Unsafe alias of a String's bytes is that String's
     * state whatever local or field names it. The JVM searches its heap for what refers
     * to the array, which no request can interrupt, so the wait is bounded: 2 s, less
     * where the session's pause budget leaves less (``pauseLeftMs``, from Peeky's worker);
     * past it, the change is refused. Fails closed wherever the JVM cannot tell.
     */
    private void checkArrayTarget(int reference, long pauseLeftMs) throws Exception {
        Object container=debugContext().getRecyclableIdPool().getObjectById(reference);
        if (!(container instanceof VariableProxy)) return;
        Object value=((VariableProxy) container).getProxiedVariable();
        if (!(value instanceof ArrayReference)) return;
        ArrayReference array=(ArrayReference) value;
        requireInstanceInfo(array);
        long wait=pauseLeftMs<0 ? SEARCH_MS : Math.min(SEARCH_MS, pauseLeftMs - PAUSE_MARGIN_MS);
        if (wait<MIN_SEARCH_MS)
            throw new IllegalStateException("only " + String.format("%.1f", pauseLeftMs / 1000.0) + " s of the "
                + "session's pause budget is left, too little to search for what refers to the "
                + "array; change it at a fresh stop");
        String holder=searchHolder(array, wait);
        if (holder!=null)
            throw new IllegalArgumentException("the array is " + holder
                + ", a JDK class's field; Peeky never changes the JDK's own state");
    }

    private static void requireInstanceInfo(ArrayReference array) {
        if (!array.virtualMachine().canGetInstanceInfo())
            throw new IllegalStateException("this JVM cannot list what refers to an array, so "
                + "Peeky cannot tell whether a JDK class holds it");
    }

    /** A search for what refers to an array that ran out of time: a longer one may tell. */
    private static final class SearchTimeout extends IllegalStateException {
        SearchTimeout(String message) {
            super(message);
        }
    }

    /**
     * Where a JDK class's field holds ``array`` (jdkHolder), or null where none does, from
     * the JVM's search for what refers to it, waiting at most ``wait`` ms. Throws
     * IllegalStateException where that cannot be told (SearchTimeout where time ran out).
     */
    private static String searchHolder(ArrayReference array, long wait) throws InterruptedException {
        requireInstanceInfo(array);
        Future<String> search=SEARCH.submit(() -> jdkHolder(array, false, null, 0, new int[1], new HashSet<>()));
        try {
            return search.get(wait, TimeUnit.MILLISECONDS);
        } catch (TimeoutException error) {
            search.cancel(true);
            throw new SearchTimeout("Peeky could not tell within " + wait + " ms whether a JDK "
                + "class holds the array (the JVM searches its heap for what refers to it)");
        } catch (ExecutionException error) {
            Throwable cause=error.getCause();
            throw new IllegalStateException(cause instanceof IllegalStateException ? cause.getMessage()
                : "Peeky could not tell whether a JDK class holds the array (" + cause + ")");
        }
    }

    /**
     * Whether a JDK class's field holds the array whose elements a listing shows,
     * asked before Peeky marks them `[settable]`: the search a write makes
     * (checkArrayTarget), with a shorter wait, so a mark promises only what a write does.
     * Body: `holder` (where a JDK class's field holds it), or `unknown` (why that cannot
     * be told) with `timedOut` (whether a longer search, as a write makes, may tell), or
     * neither (no JDK class holds it); and `allSuspended`: whether every thread of the JVM
     * was suspended, so that no other can change what holds the array before the stop
     * ends (a line breakpoint stops its own thread only).
     */
    private JsonObject arrayHolder(JsonObject args) throws Exception {
        JsonElement reference=args==null ? null : args.get("variablesReference");
        if (reference==null || !reference.isJsonPrimitive() || !reference.getAsJsonPrimitive().isNumber())
            throw new IllegalArgumentException("peekyArrayHolder requires a numeric variablesReference");
        Object container=debugContext().getRecyclableIdPool().getObjectById(reference.getAsInt());
        Object value=container instanceof VariableProxy ? ((VariableProxy) container).getProxiedVariable() : null;
        if (!(value instanceof ArrayReference))
            throw new IllegalArgumentException("peekyArrayHolder takes an array's variablesReference");
        long pauseLeftMs=args.has("peekyPauseLeftMs") ? Math.max(0, args.get("peekyPauseLeftMs").getAsLong()) : -1;
        long wait=pauseLeftMs<0 ? LIST_SEARCH_MS : Math.min(LIST_SEARCH_MS, pauseLeftMs - PAUSE_MARGIN_MS);
        JsonObject body=new JsonObject();
        boolean all=true;
        for (com.sun.jdi.ThreadReference thread : ((ArrayReference) value).virtualMachine().allThreads()) {
            if (!thread.isSuspended()) {
                all=false;
                break;
            }
        }
        body.addProperty("allSuspended", all);
        if (wait<MIN_LIST_SEARCH_MS) {
            body.addProperty("unknown", "too little of the session's pause budget is left to search "
                + "for what refers to the array");
            body.addProperty("timedOut", true);
            return body;
        }
        try {
            String holder=searchHolder((ArrayReference) value, wait);
            if (holder!=null) body.addProperty("holder", holder);
        } catch (IllegalStateException error) {
            body.addProperty("unknown", error.getMessage());
            body.addProperty("timedOut", error instanceof SearchTimeout);
        }
        return body;
    }

    /**
     * Where a JDK class's field holds ``value`` (`held by java.lang.String.value`), directly
     * or as an element of an array held so (`inside an array held by ...`), from what refers
     * to it; null where nothing does. An array ``value`` is an element of is judged the same
     * way, whatever its type: an Object[] is no proof of whose its elements are. Only the
     * backing arrays of the JDK's lists (USER_ELEMENTS) hold the program's own elements,
     * where the list itself is the program's, judged the same way: its own views (``views``:
     * its iterators and sublists) do not own it, but whoever holds one holds the list.
     * ``container``: whether ``value`` is an array holding the one being written.
     */
    private static String jdkHolder(ObjectReference value, boolean container, String views, int depth, int[] read, Set<Long> seen) {
        if (!seen.add(value.uniqueID())) return null;
        List<ObjectReference> referrers=value.referringObjects(MAX_REFERRERS + 1);
        read[0]+=referrers.size();
        if (read[0]>MAX_REFERRERS)
            throw new IllegalStateException("more than " + MAX_REFERRERS + " objects refer to the "
                + "array or the arrays holding it, so Peeky cannot tell whether a JDK class holds it");
        for (ObjectReference referrer : referrers) {
            if (referrer instanceof ArrayReference) {
                if (depth>=MAX_DEPTH)
                    throw new IllegalStateException("the array is nested too deep in others for "
                        + "Peeky to tell whether a JDK class holds it");
                String found=jdkHolder((ArrayReference) referrer, true, null, depth + 1, read, seen);
                if (found!=null) return found.startsWith("held") ? "inside an array " + found : found;
                continue;
            }
            for (com.sun.jdi.Field field : referrer.referenceType().allFields()) {
                if (field.isStatic() || !jdkLoaded(field.declaringType()) || !value.equals(referrer.getValue(field)))
                    continue;
                String owner=field.declaringType().name(), name=owner + "." + field.name();
                if (views!=null && owner.startsWith(views + "$")) {
                    // An iterator or sublist of the list being judged: not its owner, but
                    // whoever holds the view holds the list through it.
                    if (depth>=MAX_DEPTH)
                        throw new IllegalStateException("the array is nested too deep in others for "
                            + "Peeky to tell whether a JDK class holds it");
                    String found=jdkHolder(referrer, false, views, depth + 1, read, seen);
                    if (found!=null)
                        return found.startsWith("held by ")
                            ? "held, through an iterator or sublist, by " + found.substring("held by ".length())
                            : found;
                    continue;
                }
                if (container && USER_ELEMENTS.contains(name)) {
                    // A list's backing array: its elements are the program's own values,
                    // where the list is the program's.
                    if (depth>=MAX_DEPTH)
                        throw new IllegalStateException("the array is nested too deep in others for "
                            + "Peeky to tell whether a JDK class holds it");
                    String found=jdkHolder(referrer, false, owner, depth + 1, read, seen);
                    if (found!=null) return found.startsWith("held") ? "in a list " + found : found;
                    continue;
                }
                return "held by " + name;
            }
            ReferenceType reflected=referrer instanceof ClassObjectReference
                ? ((ClassObjectReference) referrer).reflectedType() : null;
            // A static field: the JVM finds it in its class's mirror.
            if (reflected!=null && jdkLoaded(reflected))
                for (com.sun.jdi.Field field : reflected.fields())
                    if (field.isStatic() && value.equals(reflected.getValue(field)))
                        return "held by " + reflected.name() + "." + field.name();
        }
        return null;
    }

    /**
     * Name the class that declares each field an object's listing shows (`peekyOwner`)
     * and whether the JDK loaded it (`peekyJdkOwner`): java-debug names fields
     * only, and Peeky never changes a field a JDK class declares, inherited or not. A name
     * two instance fields share stays unnamed.
     */
    private Object withOwners(int reference, Object body) {
        try {
            Object container=debugContext().getRecyclableIdPool().getObjectById(reference);
            Object value=container instanceof VariableProxy ? ((VariableProxy) container).getProxiedVariable() : null;
            if (!(value instanceof ObjectReference) || value instanceof ArrayReference) return body;
            Map<String, List<ReferenceType>> owners=new HashMap<>();
            for (com.sun.jdi.Field field : ((ObjectReference) value).referenceType().allFields())
                if (!field.isStatic())
                    owners.computeIfAbsent(field.name(), key -> new ArrayList<>()).add(field.declaringType());
            JsonObject json=JsonUtils.toJsonTree(body, body.getClass()).getAsJsonObject();
            for (JsonElement each : json.getAsJsonArray("variables")) {
                JsonObject variable=each.getAsJsonObject();
                List<ReferenceType> found=owners.get(variable.get("name").getAsString());
                if (found==null || found.size()!=1) continue;
                variable.addProperty("peekyOwner", found.get(0).name());
                // Whether the JDK loaded it, never judged by its package's name.
                variable.addProperty("peekyJdkOwner", jdkLoaded(found.get(0)));
            }
            return json;
        } catch (Exception error) {
            // Unnamed: Peeky then changes none of these fields.
            return body;
        }
    }

    /**
     * java-debug turns a quoted value into a String for a String variable, but parses it as
     * false for a boolean and keeps its first character for a char: allow text only where
     * the variable's declared type is java.lang.String, and a quoted char only where it is
     * char (a local, a field or an element).
     */
    private void checkDeclaredTarget(int reference, String name, String wanted, String refusal) throws ReflectiveOperationException {
        IDebugAdapterContext context=debugContext();
        Object container=context.getRecyclableIdPool().getObjectById(reference);
        Object value=container instanceof VariableProxy ? ((VariableProxy) container).getProxiedVariable() : null;
        String type=null;
        try {
            if (value instanceof StackFrameReference) {
                StackFrame frame=context.getStackFrameManager().getStackFrame((StackFrameReference) value);
                LocalVariable local=frame==null ? null : frame.visibleVariableByName(name);
                type=local==null ? null : local.typeName();
            } else if (value instanceof ArrayReference) {
                type=((ArrayType) ((ArrayReference) value).referenceType()).componentTypeName();
            } else if (value instanceof ObjectReference) {
                com.sun.jdi.Field field=((ObjectReference) value).referenceType().fieldByName(name);
                type=field==null ? null : field.typeName();
            }
        } catch (AbsentInformationException error) {
            type=null;
        }
        if (!wanted.equals(type))
            throw new IllegalArgumentException(refusal + "; " + name + " is " + type);
    }

    private IDebugAdapterContext debugContext() throws ReflectiveOperationException {
        java.lang.reflect.Field adapter=ProtocolServer.class.getDeclaredField("debugAdapter");
        adapter.setAccessible(true);
        Object debugAdapter=adapter.get(this);
        java.lang.reflect.Field context=debugAdapter.getClass().getDeclaredField("debugContext");
        context.setAccessible(true);
        return (IDebugAdapterContext) context.get(debugAdapter);
    }

    @Override public void sendResponse(Messages.Response response) {
        // A change's JVM runs again before its reply goes out, or no reply does.
        com.sun.jdi.VirtualMachine suspended=paused.remove(response.request_seq);
        if (suspended!=null) resumeOrEnd(suspended);
        if ("variables".equals(response.command)) {
            Integer reference=listings.remove(response.request_seq);
            if (response.success && reference!=null && response.body!=null)
                response.body=withOwners(reference, response.body);
        }
        if (response.success && response.command.equals("initialize")) {
            // Positive capability list: never inherit unimplemented JDT provider capabilities.
            JsonObject caps=new JsonObject();
            caps.addProperty("supportsConfigurationDoneRequest", true);
            caps.addProperty("supportsSetVariable", true);
            // Peeky's own marker: this launcher allows next/stepIn/stepOut (with fixed filters).
            caps.addProperty("peekySteps", true);
            // And refuses a field write whose name a hiding or second field shares.
            caps.addProperty("peekyFieldSet", true);
            // And changes a String to a text, only where it is declared a String.
            caps.addProperty("peekyTextSet", true);
            // And changes a char to a quoted char, only where it is declared a char.
            caps.addProperty("peekyCharSet", true);
            // And names each listed field's declaring class and whether the JDK loaded it,
            // never changing a JDK class's field or an array one holds.
            caps.addProperty("peekyJdkOwners", true);
            // And answers peekyArrayHolder: whether a JDK class's field holds an array, so
            // its elements are marked [settable] only where a write is made.
            caps.addProperty("peekyArrayHolders", true);
            response.body=caps;
        }
        super.sendResponse(response);
    }

    private static final class Entry {
        final Path path;
        final Set<Integer> lines=new HashSet<>();
        Entry(JsonObject json) throws Exception {
            path=Path.of(json.get("path").getAsString()).toRealPath();
            if (!Files.isRegularFile(path)) throw new IllegalArgumentException("source must be regular file");
            for(JsonElement n:json.getAsJsonArray("lines")) {
                int line=n.getAsInt();
                if(line<1) throw new IllegalArgumentException("line must be positive");
                lines.add(line);
            }
        }
    }
    private static final class SourceMap {
        final Map<String,Entry> classes=new HashMap<>();
        SourceMap(Path path) throws Exception {
            JsonObject json=JsonParser.parseString(Files.readString(path)).getAsJsonObject();
            for(var item:json.getAsJsonObject("classes").entrySet())
                classes.put(item.getKey(),new Entry(item.getValue().getAsJsonObject()));
            if(classes.isEmpty()) throw new IllegalArgumentException("empty source map");
        }
        String source(String className) {
            Entry e=classes.get(className);
            return e==null?null:e.path.toUri().toString();
        }
        JavaBreakpointLocation[] locations(String uri, Types.SourceBreakpoint[] bps) throws Exception {
            Path path=(uri.startsWith("file:")?Path.of(URI.create(uri)):Path.of(uri)).toRealPath();
            JavaBreakpointLocation[] result=new JavaBreakpointLocation[bps.length];
            for(int i=0;i<bps.length;i++) {
                String found=null;
                for(var entry:classes.entrySet()) {
                    if(entry.getValue().path.equals(path)&&entry.getValue().lines.contains(bps[i].line)) {
                        if(found!=null) throw new IllegalArgumentException("ambiguous class for source line");
                        found=entry.getKey();
                    }
                }
                if(found==null) throw new IllegalArgumentException("source line absent from prepared source map");
                result[i]=new JavaBreakpointLocation(bps[i].line,bps[i].column);
                result[i].setClassName(found);
            }
            return result;
        }
    }

    private static IProvider provider(Class<? extends IProvider> type, SourceMap sources) {
        return (IProvider)Proxy.newProxyInstance(type.getClassLoader(),new Class<?>[]{type},(proxy,method,args)->{
            switch(method.getName()) {
                case "initialize": case "close": case "onClassRedefined": case "clearState": return null;
                case "getVirtualMachineManager": return com.sun.jdi.Bootstrap.virtualMachineManager();
                case "isInEvaluation": case "supportsRealtimeBreakpointVerification": return false;
                case "getEventHub": return io.reactivex.Observable.never();
                case "getJavaRuntimeVersion": case "getOriginalLineMappings": case "getDecompiledLineMappings": return null;
                case "findMethodInvocations": case "codeComplete": return List.of();
                case "getSourceFileURI": return sources.source((String)args[0]);
                case "getSource": return new Source(sources.source((String)args[0]),SourceType.LOCAL);
                case "getBreakpointLocations": return sources.locations((String)args[0],(Types.SourceBreakpoint[])args[1]);
                case "toString": return "Peeky " + type.getSimpleName();
                case "hashCode": return System.identityHashCode(proxy);
                case "equals": return proxy==args[0];
                default:
                    if(CompletableFuture.class.isAssignableFrom(method.getReturnType()))
                        return CompletableFuture.failedFuture(new UnsupportedOperationException(method.getName()));
                    throw new UnsupportedOperationException(method.getName());
            }
        });
    }
    public static void main(String[] args) throws Exception {
        if(args.length!=1) throw new IllegalArgumentException("expected prepared source-map JSON path");
        SourceMap sources=new SourceMap(Path.of(args[0]));
        DebugSettings.getCurrent().showToString=false;
        DebugSettings.getCurrent().showLogicalStructure=false;
        // Bound String values (default 0 = unlimited) so one huge field cannot exceed the
        // client's DAP frame limit; the worker shows at most 2000 characters anyway.
        DebugSettings.getCurrent().maxStringLength=4096;
        ProviderContext context=new ProviderContext();
        for(Class<? extends IProvider> type:List.of(ISourceLookUpProvider.class,IEvaluationProvider.class,
            IHotCodeReplaceProvider.class,ICompletionsProvider.class,IVirtualMachineManagerProvider.class))
            context.registerProvider(type,provider(type,sources));
        new PeekyJavaAdapter(context,sources).run();
    }
}
