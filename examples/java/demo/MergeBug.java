package demo;

import java.util.ArrayList;
import java.util.List;

public class MergeBug {
    static class Interval {
        int start;
        int end;

        Interval(int start, int end) {
            this.start = start;
            this.end = end;
        }

        public String toString() {
            return "[" + start + "," + end + "]";
        }
    }

    public static void main(String[] args) {
        List<Interval> input = List.of(new Interval(1, 10), new Interval(2, 5), new Interval(12, 14));
        List<Interval> merged = new ArrayList<>();
        Interval current = input.get(0);
        for (Interval next : input.subList(1, input.size())) {
            if (next.start <= current.end) { // OBSERVE
                current.end = next.end; // BUG: should be Math.max(current.end, next.end)
            } else {
                merged.add(current);
                current = next;
            }
        }
        merged.add(current);
        System.out.println("MERGED:" + merged);
    }
}
