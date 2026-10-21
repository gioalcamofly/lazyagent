//! `LAZYAGENT_BUILD_COMMIT`: the commit the app is built from (`+dirty`
//! when `desktop/` has uncommitted changes), for "the app is behind the
//! checkout" (lazyagent update). Empty outside a git checkout.

use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn main() {
    let commit = git(&["rev-parse", "HEAD"]).unwrap_or_default();
    let dirty = !commit.is_empty() && git(&["status", "--porcelain", "--", "../.."]).is_some_and(|s| !s.is_empty());
    let stamp = if dirty { format!("{commit}+dirty") } else { commit };
    println!("cargo:rustc-env=LAZYAGENT_BUILD_COMMIT={stamp}");
    if let Some(dir) = git(&["rev-parse", "--absolute-git-dir"]) {
        println!("cargo:rerun-if-changed={dir}/logs/HEAD");
        println!("cargo:rerun-if-changed={dir}/HEAD");
    }
    println!("cargo:rerun-if-changed=src");
}
