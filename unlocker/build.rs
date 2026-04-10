use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::SystemTime;

fn main() {
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let profile = env::var("PROFILE").unwrap_or_else(|_| "debug".into());
    let target_profile = manifest.join("target").join(&profile);
    let build_dir = target_profile.join("build");

    // steamworks-sys may leave multiple build dirs across rebuilds; take the
    // newest by mtime so we always copy the current redistributable.
    let mut newest: Option<(PathBuf, SystemTime)> = None;
    if let Ok(entries) = fs::read_dir(&build_dir) {
        for entry in entries.flatten() {
            if !entry
                .file_name()
                .to_string_lossy()
                .starts_with("steamworks-sys-")
            {
                continue;
            }
            let src = entry.path().join("out").join("libsteam_api.so");
            if let Ok(meta) = src.metadata() {
                if let Ok(mtime) = meta.modified() {
                    if newest.as_ref().map(|(_, t)| mtime > *t).unwrap_or(true) {
                        newest = Some((src, mtime));
                    }
                }
            }
        }
    }

    match newest {
        Some((src, _)) => {
            let dst = target_profile.join("libsteam_api.so");
            if let Err(e) = fs::copy(&src, &dst) {
                println!("cargo:warning=failed to copy libsteam_api.so: {e}");
            } else {
                println!("cargo:rerun-if-changed={}", src.display());
            }
        }
        None => {
            println!(
                "cargo:warning=libsteam_api.so not found under target/{}/build/steamworks-sys-*/out/",
                profile
            );
        }
    }
}
