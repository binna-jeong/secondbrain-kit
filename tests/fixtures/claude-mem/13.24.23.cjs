// trimmed from claude-mem@13.24.23 plugin/scripts/worker-service.cjs (definitions + buildLauncherPrefix only)
var q1=1,xPe=["onnxruntime>=1.20","protobuf<7"],z2=2;var wPe="0.2.6",y3=3;
class Gn{static x(){}static buildLauncherPrefix(e){let r=xPe.flatMap(n=>["--with",n]);return["--python",e,...r,"--from",`chroma-mcp==${wPe}`,"chroma-mcp"]}static buildPrewarmCommandArgs(e){return e}}
