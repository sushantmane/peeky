package demo;

import java.util.Arrays;

public class PaginationBug {
    public static void main(String[] args) {
        int page = args.length > 0 ? Integer.parseInt(args[0]) : 1;
        int pageSize = args.length > 1 ? Integer.parseInt(args[1]) : 3;
        int total = args.length > 2 ? Integer.parseInt(args[2]) : 8;
        int[] items = new int[total];
        for (int i = 0; i < total; i++) items[i] = (i + 1) * 10;
        int start = page * pageSize;
        int end = Math.min(start + pageSize, items.length); // OBSERVE
        int[] result = Arrays.copyOfRange(items, Math.min(start, items.length), end);
        System.out.println("PAGE:" + Arrays.toString(result));
    }
}
