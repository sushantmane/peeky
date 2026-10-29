use std::collections::{BTreeMap, HashMap};

use crate::catalog::{Catalog, Cents};
use crate::customers::Customer;
use crate::orders::Order;

#[derive(Debug, Clone)]
pub enum Rule {
    PercentOff(i64),
    FixedOff { cents: Cents, minimum: Cents },
    TagBulk { tag: String, units: u32, percent: i64 },
}

#[derive(Debug, Default, Clone)]
pub struct Quote {
    pub order_id: u32,
    pub subtotal: Cents,
    pub discount: Cents,
    pub shipping: Cents,
    pub notes: Vec<String>,
}

impl Quote {
    pub fn total(&self) -> Cents {
        self.subtotal - self.discount + self.shipping
    }
}

pub trait ShippingRule {
    fn charge(&self, catalog: &Catalog, order: &Order, subtotal: Cents) -> Cents;
}

struct FreeOver(Cents, Cents);

impl ShippingRule for FreeOver {
    fn charge(&self, _: &Catalog, _: &Order, subtotal: Cents) -> Cents {
        if subtotal >= self.0 { 0 } else { self.1 }
    }
}

struct Heavy { free_grams: u32, per_kilo: Cents }

impl ShippingRule for Heavy {
    fn charge(&self, catalog: &Catalog, order: &Order, _: Cents) -> Cents {
        let grams: u32 = order
            .lines
            .iter()
            .filter_map(|line| catalog.find(&line.sku).map(|item| item.weight_grams * line.quantity))
            .sum();
        if grams > self.free_grams { ((grams - self.free_grams) / 1000) as Cents * self.per_kilo } else { 0 }
    }
}

pub struct Pricing<'a> {
    catalog: &'a Catalog,
    coupons: HashMap<String, Rule>,
    tiers: BTreeMap<String, i64>,
    shipping: Vec<Box<dyn ShippingRule>>,
}

impl<'a> Pricing<'a> {
    pub fn new(catalog: &'a Catalog) -> Self {
        Pricing {
            catalog,
            coupons: HashMap::new(),
            tiers: BTreeMap::new(),
            shipping: vec![Box::new(FreeOver(10000, 799)), Box::new(Heavy { free_grams: 5000, per_kilo: 150 })],
        }
    }

    pub fn add_coupon(&mut self, code: &str, rule: Rule) {
        self.coupons.insert(code.to_string(), rule);
    }

    pub fn add_tier(&mut self, tier: &str, percent: i64) {
        self.tiers.insert(tier.to_string(), percent);
    }

    fn discount_for(&self, rule: &Rule, order: &Order, subtotal: Cents) -> Cents {
        match rule {
            Rule::PercentOff(percent) => subtotal * percent / 100,
            Rule::FixedOff { cents, minimum } => if subtotal >= *minimum { *cents } else { 0 },
            Rule::TagBulk { tag, units, percent } => {
                let (count, tagged) = order
                    .lines
                    .iter()
                    .filter_map(|line| self.catalog.find(&line.sku).map(|item| (item, line.quantity)))
                    .filter(|(item, _)| item.tags.contains(tag))
                    .fold((0u32, 0 as Cents), |(n, sum), (item, quantity)| {
                        (n + quantity, sum + item.unit_price * quantity as Cents)
                    });
                if count >= *units { tagged * percent / 100 } else { 0 }
            }
        }
    }

    pub fn quote(&self, order: &Order, customer: &Customer) -> Quote {
        let mut quote = Quote { order_id: order.id, ..Quote::default() };
        let mut by_sku: BTreeMap<&str, Cents> = BTreeMap::new();
        for line in &order.lines {
            match self.catalog.find(&line.sku) {
                Some(item) => *by_sku.entry(line.sku.as_str()).or_default() += item.unit_price * line.quantity as Cents,
                None => quote.notes.push(format!("unknown sku {}", line.sku)),
            }
        }
        quote.subtotal = by_sku.values().sum();
        let tier_discount = self.tiers.get(&customer.tier).map(|p| quote.subtotal * p / 100).unwrap_or(0);
        let coupon_discount = match &order.coupon {
            Some(code) => match self.coupons.get(code) {
                Some(rule) => self.discount_for(rule, order, quote.subtotal),
                None => {
                    quote.notes.push(format!("unknown coupon {code}"));
                    0
                }
            },
            None => 0,
        };
        quote.discount = tier_discount.max(coupon_discount); // OBSERVE
        quote.shipping = self.shipping.iter().map(|rule| rule.charge(self.catalog, order, quote.subtotal)).sum();
        quote
    }
}
