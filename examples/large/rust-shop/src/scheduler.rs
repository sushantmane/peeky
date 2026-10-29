use std::cmp::Ordering;
use std::collections::BinaryHeap;

use crate::catalog::Cents;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Shipment {
    pub order_id: u32,
    pub priority: u8,
    pub day: u32,
    pub value: Cents,
}

#[derive(Debug, PartialEq, Eq)]
struct Pending {
    priority: u8,
    value: Cents,
    order_id: u32,
}

impl Ord for Pending {
    fn cmp(&self, other: &Self) -> Ordering {
        self.priority
            .cmp(&other.priority)
            .then(self.value.cmp(&other.value))
            .then(other.order_id.cmp(&self.order_id))
    }
}

impl PartialOrd for Pending {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

pub struct Scheduler {
    per_day: u32,
    queue: BinaryHeap<Pending>,
}

impl Scheduler {
    pub fn new(per_day: u32) -> Self {
        Scheduler { per_day: per_day.max(1), queue: BinaryHeap::new() }
    }

    pub fn submit(&mut self, order_id: u32, priority: u8, value: Cents) {
        self.queue.push(Pending { priority, value, order_id });
    }

    pub fn plan(&mut self) -> Vec<Shipment> {
        let mut plan = Vec::with_capacity(self.queue.len());
        let (mut day, mut today) = (0, 0);
        while let Some(next) = self.queue.pop() {
            if today == self.per_day {
                day += 1;
                today = 0;
            }
            plan.push(Shipment { order_id: next.order_id, priority: next.priority, day, value: next.value });
            today += 1;
        }
        plan
    }
}
