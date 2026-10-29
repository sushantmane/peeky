mod catalog;
mod customers;
mod inventory;
mod orders;
mod pricing;
mod report;
mod scheduler;

use inventory::Inventory;
use pricing::{Pricing, Rule};
use scheduler::Scheduler;

fn main() {
    let per_day: u32 = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(3);
    let catalog = catalog::demo();
    let mut inventory = Inventory::new(&catalog);
    for sku in ["BLT-10", "NUT-10", "WSH-10", "SCR-07", "ANC-08"] {
        inventory.receive(sku, 500).unwrap();
    }
    for sku in ["DRL-01", "SAW-02", "GLV-03", "HLM-04", "TAP-05", "LVL-06", "GOG-09"] {
        inventory.receive(sku, 6).unwrap();
    }
    let customers = customers::demo();
    let mut pricing = Pricing::new(&catalog);
    pricing.add_coupon("SPRING10", Rule::PercentOff(10));
    pricing.add_coupon("SAFETY5", Rule::TagBulk { tag: "safety".into(), units: 10, percent: 5 });
    pricing.add_coupon("BULK", Rule::FixedOff { cents: 2500, minimum: 20000 });
    pricing.add_tier("gold", 8);
    pricing.add_tier("silver", 4);

    let (orders, mut errors) = orders::parse_orders(orders::DEMO);
    let mut quotes = Vec::new();
    let mut scheduler = Scheduler::new(per_day);
    for order in &orders {
        if let Err(shortages) = inventory.reserve(order) {
            errors.extend(shortages.into_iter().map(|s| format!("order {}: {s}", order.id)));
            continue;
        }
        let Some(customer) = customers.find(order.customer_id) else {
            inventory.release(order);
            errors.push(format!("order {}: unknown customer", order.id));
            continue;
        };
        let quote = pricing.quote(order, &customer.borrow());
        customers.record(order);
        scheduler.submit(order.id, order.priority, quote.total());
        quotes.push(quote);
    }
    let plan = scheduler.plan();
    let summary = report::summarize(&quotes, &plan);
    print!("{}", report::render(&summary, &quotes));
    for (name, units) in customers.top_buyers(2) {
        println!("top {name} {units}");
    }
    for (sku, left) in inventory.low_stock(3) {
        println!("low {sku} {left}");
    }
    for error in errors {
        println!("error {error}");
    }
    let _ = catalog.tagged("tools").len() + catalog.len();
}
