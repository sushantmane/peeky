use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub struct Line {
    pub sku: String,
    pub quantity: u32,
}

#[derive(Debug, Clone)]
pub struct Order {
    pub id: u32,
    pub customer_id: u32,
    pub lines: Vec<Line>,
    pub coupon: Option<String>,
    pub priority: u8,
}

#[derive(Debug)]
pub enum ParseError {
    Fields(String),
    Number(String),
    Line(String),
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::Fields(line) => write!(f, "expected id|customer|lines[|coupon][|priority]: {line}"),
            ParseError::Number(text) => write!(f, "bad number: {text}"),
            ParseError::Line(text) => write!(f, "bad line: {text}"),
        }
    }
}

fn number<T: std::str::FromStr>(text: &str) -> Result<T, ParseError> {
    text.trim().parse().map_err(|_| ParseError::Number(text.to_string()))
}

pub fn parse_order(line: &str) -> Result<Order, ParseError> {
    let fields: Vec<&str> = line.split('|').map(str::trim).collect();
    if fields.len() < 3 {
        return Err(ParseError::Fields(line.to_string()));
    }
    let mut lines = Vec::new();
    for entry in fields[2].split(',') {
        let (sku, quantity) = entry
            .rsplit_once('x')
            .ok_or_else(|| ParseError::Line(entry.to_string()))?;
        lines.push(Line { sku: sku.trim().to_string(), quantity: number(quantity)? });
    }
    Ok(Order {
        id: number(fields[0])?,
        customer_id: number(fields[1])?,
        lines,
        coupon: fields.get(3).filter(|c| !c.is_empty()).map(|c| c.to_string()),
        priority: fields.get(4).map(|p| number(p)).transpose()?.unwrap_or(0),
    })
}

pub fn parse_orders(text: &str) -> (Vec<Order>, Vec<String>) {
    let mut orders = Vec::new();
    let mut errors = Vec::new();
    for line in text.lines().filter(|line| !line.is_empty() && !line.starts_with('#')) {
        match parse_order(line) {
            Ok(order) => orders.push(order),
            Err(error) => errors.push(error.to_string()),
        }
    }
    (orders, errors)
}

pub const DEMO: &str = "# id|customer|sku x qty,...|coupon|priority
1001|7|BLT-10x200,NUT-10x200,WSH-10x400||1
1002|3|DRL-01x1,GLV-03x2|SPRING10|2
1003|7|SAW-02x1,TAP-05x1,LVL-06x1||0
1004|9|HLM-04x5,GOG-09x5,GLV-03x5|SAFETY5|3
1005|3|SCR-07x1000,ANC-08x250||1
1006|12|DRL-01x2,SAW-02x2|BULK|0
1007|9|BLT-10x50,NUT-10x50||2
1008|7|TAP-05x3,LVL-06x2,GLV-03x1|SPRING10|1
";
