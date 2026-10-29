use std::collections::{BTreeMap, BTreeSet, HashMap};

pub type Cents = i64;

#[derive(Debug, Clone)]
pub struct Item {
    pub sku: String,
    pub name: String,
    pub unit_price: Cents,
    pub weight_grams: u32,
    pub tags: BTreeSet<String>,
}

#[derive(Debug, Default)]
pub struct Catalog {
    items: HashMap<String, Item>,
    by_tag: BTreeMap<String, Vec<String>>,
}

impl Catalog {
    pub fn add(&mut self, item: Item) {
        for tag in &item.tags {
            self.by_tag.entry(tag.clone()).or_default().push(item.sku.clone());
        }
        self.items.insert(item.sku.clone(), item);
    }

    pub fn find(&self, sku: &str) -> Option<&Item> {
        self.items.get(sku)
    }

    pub fn tagged(&self, tag: &str) -> Vec<&Item> {
        let mut found: Vec<&Item> = self
            .by_tag
            .get(tag)
            .map(|skus| skus.iter().filter_map(|sku| self.find(sku)).collect())
            .unwrap_or_default();
        found.sort_by(|a, b| a.sku.cmp(&b.sku));
        found
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }
}

pub fn demo() -> Catalog {
    let rows: [(&str, &str, Cents, u32, &[&str]); 12] = [
        ("BLT-10", "Bolt M10", 45, 12, &["hardware", "bulk"]),
        ("NUT-10", "Nut M10", 20, 6, &["hardware", "bulk"]),
        ("WSH-10", "Washer M10", 8, 2, &["hardware", "bulk"]),
        ("DRL-01", "Cordless drill", 8999, 1650, &["tools", "power"]),
        ("SAW-02", "Hand saw", 2450, 720, &["tools"]),
        ("GLV-03", "Work gloves", 1299, 140, &["safety", "apparel"]),
        ("HLM-04", "Hard hat", 2199, 410, &["safety"]),
        ("TAP-05", "Measuring tape", 1599, 260, &["tools"]),
        ("LVL-06", "Spirit level", 1899, 530, &["tools"]),
        ("SCR-07", "Screw 4x40", 5, 3, &["hardware", "bulk"]),
        ("ANC-08", "Wall anchor", 12, 2, &["hardware", "bulk"]),
        ("GOG-09", "Safety goggles", 999, 90, &["safety", "apparel"]),
    ];
    let mut catalog = Catalog::default();
    for (sku, name, unit_price, weight_grams, tags) in rows {
        catalog.add(Item {
            sku: sku.to_string(),
            name: name.to_string(),
            unit_price,
            weight_grams,
            tags: tags.iter().map(|tag| tag.to_string()).collect(),
        });
    }
    catalog
}
