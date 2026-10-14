#[derive(Debug)]
struct Request {
    page: i32,
    page_size: i32,
    total: i32,
}

#[derive(Debug)]
enum Mode {
    First,
    Later,
}

fn bounded(text: Option<&String>, default: i32, minimum: i32) -> i32 {
    let value = text.map_or(Ok(default), |s| s.parse::<i32>()).unwrap_or_else(|_| std::process::exit(2));
    if !(minimum..=128).contains(&value) {
        std::process::exit(2);
    }
    value
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() > 3 {
        std::process::exit(2);
    }
    let request = Request {
        page: bounded(args.first(), 1, 1),
        page_size: bounded(args.get(1), 3, 1),
        total: bounded(args.get(2), 8, 0),
    };
    let mode = if request.page == 1 { Mode::First } else { Mode::Later };
    let some: Option<i32> = Some(request.page_size);
    let none: Option<i32> = None;
    let mut start: i32 = request.page * request.page_size;
    let end = (start + request.page_size).min(request.total); // OBSERVE
    let values: Vec<i32> = (start..end).map(|index| (index + 1) * 10).collect();
    println!("PAGE:{values:?}");
    // Keep representative locals alive through the observation point.
    std::hint::black_box((&request, &mode, &some, &none, &mut start));
}
