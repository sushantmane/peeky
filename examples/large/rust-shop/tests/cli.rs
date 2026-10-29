use std::process::Command;

#[test]
fn reports_revenue() {
    let output = Command::new(env!("CARGO_BIN_EXE_shop")).arg("3").output().unwrap();
    assert!(output.status.success());
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("revenue $666.51"), "{text}");
}
