use std::collections::BTreeMap;
use std::rc::Rc;
use std::cell::RefCell;

use crate::orders::Order;

#[derive(Debug, Default)]
pub struct Customer {
    pub id: u32,
    pub name: String,
    pub tier: String,
    pub history: BTreeMap<String, u32>,
}

#[derive(Default)]
pub struct Customers {
    by_id: BTreeMap<u32, Rc<RefCell<Customer>>>,
}

impl Customers {
    pub fn add(&mut self, id: u32, name: &str, tier: &str) {
        let customer = Customer { id, name: name.into(), tier: tier.into(), ..Customer::default() };
        self.by_id.insert(id, Rc::new(RefCell::new(customer)));
    }

    pub fn find(&self, id: u32) -> Option<Rc<RefCell<Customer>>> {
        self.by_id.get(&id).cloned()
    }

    pub fn record(&self, order: &Order) {
        if let Some(customer) = self.by_id.get(&order.customer_id) {
            let mut customer = customer.borrow_mut();
            for line in &order.lines {
                *customer.history.entry(line.sku.clone()).or_default() += line.quantity;
            }
        }
    }

    pub fn top_buyers(&self, count: usize) -> Vec<(String, u32)> {
        let mut totals: Vec<(String, u32)> = self
            .by_id
            .values()
            .map(|c| {
                let c = c.borrow();
                (c.name.clone(), c.history.values().sum())
            })
            .collect();
        totals.sort_by(|a, b| b.1.cmp(&a.1));
        totals.truncate(count);
        totals
    }
}

pub fn demo() -> Customers {
    let mut customers = Customers::default();
    customers.add(3, "Ana Builders", "gold");
    customers.add(7, "Bo's Hardware", "silver");
    customers.add(9, "City Works", "public");
    customers.add(12, "Dee Renovations", "");
    customers
}
