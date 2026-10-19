package demo;

import java.util.ArrayList;
import java.util.List;

public class BalanceBug {
    static class Worker {
        final String name;
        int load;

        Worker(String name, int load) {
            this.name = name;
            this.load = load;
        }
    }

    /** Gives each task to the least-loaded worker; ties go to the alphabetically first name. */
    static List<String> assign(List<Worker> workers, int[] tasks) {
        List<String> order = new ArrayList<>();
        for (int task : tasks) {
            Worker best = null;
            for (Worker w : workers) {
                if (best == null || w.load < best.load) {
                    best = w;
                } else if (w.load == best.load && w.name.compareTo(best.name) < 0) {
                    best = new Worker(w.name, w.load);
                }
            }
            best.load += task;
            order.add(best.name);
        }
        return order;
    }

    public static void main(String[] args) {
        List<Worker> workers = new ArrayList<>();
        for (String name : args[0].split(",")) {
            workers.add(new Worker(name, 0));
        }
        String[] parts = args[1].split(",");
        int[] tasks = new int[parts.length];
        for (int i = 0; i < parts.length; i++) {
            tasks[i] = Integer.parseInt(parts[i]);
        }
        System.out.println("ORDER:" + String.join(" ", assign(workers, tasks)));
    }
}
