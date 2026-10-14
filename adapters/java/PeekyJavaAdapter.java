import com.google.gson.*;
import com.microsoft.java.debug.core.DebugSettings;
import com.microsoft.java.debug.core.JavaBreakpointLocation;
import com.microsoft.java.debug.core.adapter.*;
import com.microsoft.java.debug.core.protocol.Messages;
import com.microsoft.java.debug.core.protocol.Types;
import java.lang.reflect.Proxy;
import java.net.URI;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.CompletableFuture;

/** Experimental Microsoft-core launcher. Only explicit, prepared source maps are accepted. */
public final class PeekyJavaAdapter extends ProtocolServer {
    private static final Set<String> COMMANDS = Set.of("initialize", "launch", "setBreakpoints",
        "configurationDone", "threads", "stackTrace", "scopes", "variables", "setVariable",
        "continue", "disconnect");
    private final SourceMap sources;

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
                if (!value.matches("(?:true|false|[+-]?[0-9]+(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"))
                    throw new IllegalArgumentException("only numeric or boolean scalar literals supported");
            }
            super.dispatchRequest(request);
        } catch (Exception error) {
            sendResponse(new Messages.Response(request.seq, request.command, false,
                "Peeky experimental Java adapter: " + error.getMessage()));
        }
    }

    @Override public void sendResponse(Messages.Response response) {
        if (response.success && response.command.equals("initialize")) {
            // Positive capability list: never inherit unimplemented JDT provider capabilities.
            JsonObject caps=new JsonObject();
            caps.addProperty("supportsConfigurationDoneRequest", true);
            caps.addProperty("supportsSetVariable", true);
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
        ProviderContext context=new ProviderContext();
        for(Class<? extends IProvider> type:List.of(ISourceLookUpProvider.class,IEvaluationProvider.class,
            IHotCodeReplaceProvider.class,ICompletionsProvider.class,IVirtualMachineManagerProvider.class))
            context.registerProvider(type,provider(type,sources));
        new PeekyJavaAdapter(context,sources).run();
    }
}
