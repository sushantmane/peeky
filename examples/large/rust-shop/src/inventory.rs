use std::collections::{BTreeMap, HashMap, VecDeque};

use crate::catalog::Catalog;
use crate::orders::Order;

#[derive(Debug, Default, Clone, Copy)]
pub struct Stock {
    pub on_hand: i32,
    pub reserved: i32,
}

impl Stock {
    pub fn available(&self) -> i32 {
        self.on_hand - self.reserved
    }
}

pub struct Inventory<'a> {
    catalog: &'a Catalog,
    stock: HashMap<String, Stock>,
    journal: VecDeque<(String, i32)>,
}

impl<'a> Inventory<'a> {
    pub fn new(catalog: &'a Catalog) -> Self {
        Inventory { catalog, stock: HashMap::new(), journal: VecDeque::new() }
    }

    pub fn receive(&mut self, sku: &str, units: i32) -> Result<(), String> {
        if self.catalog.find(sku).is_none() {
            return Err(format!("unknown sku {sku}"));
        }
        self.stock.entry(sku.to_string()).or_default().on_hand += units;
        self.journal.push_back((sku.to_string(), units));
        if self.journal.len() > 64 {
            self.journal.pop_front();
        }
        Ok(())
    }

    pub fn reserve(&mut self, order: &Order) -> Result<(), Vec<String>> {
        let mut wanted: HashMap<&str, i32> = HashMap::new();
        for line in &order.lines {
            *wanted.entry(line.sku.as_str()).or_default() += line.quantity as i32;
        }
        let mut shortages: Vec<String> = wanted
            .iter()
            .filter_map(|(sku, units)| {
                let available = self.stock.get(*sku).map(Stock::available).unwrap_or(0);
                (available < *units).then(|| format!("{sku} short by {}", units - available))
            })
            .collect();
        if !shortages.is_empty() {
            shortages.sort();
            return Err(shortages);
        }
        for (sku, units) in wanted {
            self.stock.entry(sku.to_string()).or_default().reserved += units;
            self.journal.push_back((sku.to_string(), -units));
        }
        Ok(())
    }

    pub fn release(&mut self, order: &Order) {
        for line in &order.lines {
            if let Some(entry) = self.stock.get_mut(&line.sku) {
                entry.reserved = (entry.reserved - line.quantity as i32).max(0);
            }
        }
    }

    pub fn low_stock(&self, threshold: i32) -> BTreeMap<&str, i32> {
        self.stock
            .iter()
            .filter(|(_, stock)| stock.available() < threshold)
            .map(|(sku, stock)| (sku.as_str(), stock.available()))
            .collect()
    }
}
