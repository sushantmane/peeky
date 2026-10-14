use std::process::Command;

#[test]
fn first_page_starts_at_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_peeky-pagination"))
        .args(["1", "3", "8"])
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(String::from_utf8(output.stdout).unwrap(), "PAGE:[10, 20, 30]\n");
}
