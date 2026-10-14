package demo;
public class BranchBug {
    public static void main(String[] args) {
        int timeoutMs = 0;
        int[] retryDelays = {10, 20, 40};
        String result = timeoutMs > 0 ? "ready" : "timed out";
        System.out.println(result);
    }
}
