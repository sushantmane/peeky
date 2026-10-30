use std::collections::BTreeMap;
use std::fmt::Write;

use crate::catalog::Cents;
use crate::pricing::Quote;
use crate::scheduler::Shipment;

#[derive(Debug, Default)]
pub struct Summary {
    pub revenue: Cents,
    pub discounts: Cents,
    pub mean_order: f64,
    pub median_order: Cents,
    pub per_day: BTreeMap<u32, usize>,
}

pub fn money(cents: Cents) -> String {
    format!("{}${}.{:02}", if cents < 0 { "-" } else { "" }, cents.abs() / 100, cents.abs() % 100)
}

pub fn summarize(quotes: &[Quote], plan: &[Shipment]) -> Summary {
    let mut summary = Summary::default();
    let mut totals: Vec<Cents> = quotes.iter().map(Quote::total).collect();
    summary.revenue = totals.iter().sum();
    summary.discounts = quotes.iter().map(|q| q.discount).sum();
    if !totals.is_empty() {
        summary.mean_order = summary.revenue as f64 / totals.len() as f64;
        let middle = totals.len() / 2;
        summary.median_order = *totals.select_nth_unstable(middle).1;
    }
    for shipment in plan {
        *summary.per_day.entry(shipment.day).or_default() += 1;
    }
    summary
}

pub fn render(summary: &Summary, quotes: &[Quote]) -> String {
    let mut out = String::new();
    for quote in quotes {
        let _ = write!(
            out,
            "order {}: {} - {} + {} = {}",
            quote.order_id,
            money(quote.subtotal),
            money(quote.discount),
            money(quote.shipping),
            money(quote.total())
        );
        for note in &quote.notes {
            let _ = write!(out, " [{note}]");
        }
        out.push('\n');
    }
    let _ = writeln!(
        out,
        "revenue {}, discounts {}, mean {:.2}, median {}",
        money(summary.revenue),
        money(summary.discounts),
        summary.mean_order / 100.0,
        money(summary.median_order)
    );
    for (day, count) in &summary.per_day {
        let _ = writeln!(out, "day {day}: {count}");
    }
    out
}
