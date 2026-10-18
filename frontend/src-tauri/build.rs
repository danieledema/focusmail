fn main() {
    tauri_build::build();
    let target = std::env::var("TARGET").expect("Rust target missing");
    let extension = if target.contains("windows") { ".exe" } else { "" };
    let source = std::path::PathBuf::from(format!("binaries/focusmail-backend-{target}{extension}"));
    println!("cargo:rerun-if-changed={}", source.display());
    let out = std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let profile = out.ancestors().nth(3).expect("Cargo profile directory missing");
    std::fs::copy(source, profile.join(format!("focusmail-backend{extension}")))
        .expect("Prepare the Python backend with desktop:prepare before compiling");
}
