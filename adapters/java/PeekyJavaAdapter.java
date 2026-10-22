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
import java.util.regex.Pattern;

/** Experimental Microsoft-core launcher. Only explicit, prepared source maps are accepted. */
public final class PeekyJavaAdapter extends ProtocolServer {
    private static final Set<String> COMMANDS = Set.of("initialize", "launch", "attach", "setBreakpoints",
        "configurationDone", "threads", "stackTrace", "scopes", "variables", "setVariable",
        "continue", "next", "stepIn", "stepOut", "disconnect");
    private static final Set<String> STEPS = Set.of("next", "stepIn", "stepOut");
    /** The JDK's own packages (Peeky #204): their classes' fields are never written. */
    private static final Pattern JDK = Pattern.compile("(?:java|javax|jdk|sun|com\\.sun)\\.");

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
    /** The container each pending `variables` request lists, by request seq (#204). */
    private final Map<Integer, Integer> listings = new ConcurrentHashMap<>();

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
                // A text between plain quotes (Peeky #137): java-debug takes them off and makes
                // a new String of the rest, for a String variable only (checked below).
                boolean text=value.length()>=2 && value.startsWith("\"") && value.endsWith("\"");
                // One UTF-16 unit between single quotes (Peeky #183): java-debug sets a char to
                // it, for a char variable only (checked below).
                boolean character=value.length()==3 && value.startsWith("'") && value.endsWith("'");
                if (!text && !character && !value.matches("(?:true|false|[+-]?[0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"))
                    throw new IllegalArgumentException("only numeric or boolean scalar literals, a quoted char for a char, or text for a String, supported");
                checkFieldTarget(args.get("variablesReference").getAsInt(), args.get("name").getAsString());
                if (text) checkDeclaredTarget(args.get("variablesReference").getAsInt(), args.get("name").getAsString(), "java.lang.String", "text changes only a String");
                if (character) checkDeclaredTarget(args.get("variablesReference").getAsInt(), args.get("name").getAsString(), "char", "a quoted char changes only a char");
            }
            if (request.command.equals("variables") && args!=null && args.has("variablesReference")
                && args.get("variablesReference").isJsonPrimitive())
                listings.put(request.seq, args.get("variablesReference").getAsInt());
            super.dispatchRequest(request);
        } catch (Exception error) {
            sendResponse(new Messages.Response(request.seq, request.command, false,
                "Peeky experimental Java adapter: " + error.getMessage()));
        }
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
        // A JDK class's own state (#204): a String's cached hash, an ArrayList's size.
        String owner=visible.declaringType().name();
        if (JDK.matcher(owner).lookingAt())
            throw new IllegalArgumentException("field " + name + " is declared by " + owner
                + ", a JDK class; Peeky never changes the JDK's own fields");
    }

    /**
     * Name the class that declares each field an object's listing shows (`peekyOwner`,
     * Peeky #204): java-debug names fields only, and Peeky never changes a field a JDK
     * class declares, inherited or not. A name two instance fields share stays unnamed.
     */
    private Object withOwners(int reference, Object body) {
        try {
            Object container=debugContext().getRecyclableIdPool().getObjectById(reference);
            Object value=container instanceof VariableProxy ? ((VariableProxy) container).getProxiedVariable() : null;
            if (!(value instanceof ObjectReference) || value instanceof ArrayReference) return body;
            Map<String, List<String>> owners=new HashMap<>();
            for (com.sun.jdi.Field field : ((ObjectReference) value).referenceType().allFields())
                if (!field.isStatic())
                    owners.computeIfAbsent(field.name(), key -> new ArrayList<>()).add(field.declaringType().name());
            JsonObject json=JsonUtils.toJsonTree(body, body.getClass()).getAsJsonObject();
            for (JsonElement each : json.getAsJsonArray("variables")) {
                JsonObject variable=each.getAsJsonObject();
                List<String> found=owners.get(variable.get("name").getAsString());
                if (found!=null && found.size()==1) variable.addProperty("peekyOwner", found.get(0));
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
            // And changes a String to a text (#137), only where it is declared a String.
            caps.addProperty("peekyTextSet", true);
            // And changes a char to a quoted char (#183), only where it is declared a char.
            caps.addProperty("peekyCharSet", true);
            // And names each listed field's declaring class, never changing a JDK class's (#204).
            caps.addProperty("peekyFieldOwners", true);
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
