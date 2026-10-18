//! Keeping an idle offload GPU's Vulkan driver out of the process.
//!
//! The Vulkan loader initialises every installed driver while wgpu looks
//! for an adapter. On a hybrid laptop whose display runs on the integrated
//! GPU, NVIDIA's driver then keeps background threads that poll every
//! 10-100 ms: measured 1.2-1.4 % of a core with the app idle (0.2 % without
//! it) and 20-30 MB more private memory, for a GPU we never draw with.
//!
//! So when every connected output belongs to a non-NVIDIA DRM driver and
//! an NVIDIA card exists, the loader is pointed at the other drivers only
//! (`VK_ICD_FILENAMES`). Not when the user chose drivers already, or set
//! `LAZYAGENT_DESKTOP_ALL_GPUS=1`.

use std::path::{Path, PathBuf};

pub const ALL_GPUS_ENV: &str = "LAZYAGENT_DESKTOP_ALL_GPUS";
pub const ICD_ENV: &str = "VK_ICD_FILENAMES";
const ICD_DIRS: &[&str] = &["/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d", "/usr/local/share/vulkan/icd.d"];

/// DRM driver of each card, and whether one of its outputs is connected.
fn cards(drm: &Path) -> Vec<(String, bool)> {
    let Ok(entries) = std::fs::read_dir(drm) else { return vec![] };
    let names: Vec<String> =
        entries.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).collect();
    names
        .iter()
        .filter(|n| n.starts_with("card") && !n.contains('-'))
        .map(|card| {
            let driver = std::fs::read_link(drm.join(card).join("device/driver"))
                .ok()
                .and_then(|p| p.file_name().map(|f| f.to_string_lossy().into_owned()))
                .unwrap_or_default();
            let connected = names.iter().filter(|n| n.starts_with(&format!("{card}-"))).any(|n| {
                std::fs::read_to_string(drm.join(n).join("status")).map(|s| s.trim() == "connected").unwrap_or(false)
            });
            (driver, connected)
        })
        .collect()
}

/// The ICD files to keep, or `None` to leave the loader alone.
pub fn icds_without_offload_nvidia(drm: &Path, icd_dirs: &[PathBuf]) -> Option<Vec<PathBuf>> {
    let cards = cards(drm);
    let nvidia = |d: &str| d == "nvidia" || d == "nouveau";
    let has_nvidia = cards.iter().any(|(d, _)| nvidia(d));
    let any_connected = cards.iter().any(|(_, c)| *c);
    let nvidia_drives_a_display = cards.iter().any(|(d, c)| nvidia(d) && *c);
    if !has_nvidia || !any_connected || nvidia_drives_a_display {
        return None;
    }
    let mut keep: Vec<PathBuf> = icd_dirs
        .iter()
        .filter_map(|d| std::fs::read_dir(d).ok())
        .flatten()
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .filter(|p| !p.file_name().unwrap().to_string_lossy().to_lowercase().contains("nvidia"))
        .collect();
    keep.sort();
    (!keep.is_empty()).then_some(keep)
}

/// Applies the rule to this process; returns the value it set, if any.
/// Call before any thread starts and before wgpu loads.
pub fn hide_offload_nvidia() -> Option<String> {
    let set = |n: &str| std::env::var_os(n).is_some_and(|v| !v.is_empty());
    if set(ICD_ENV) || set("VK_DRIVER_FILES") || std::env::var(ALL_GPUS_ENV).as_deref() == Ok("1") {
        return None;
    }
    let dirs: Vec<PathBuf> = ICD_DIRS.iter().map(PathBuf::from).collect();
    let keep = icds_without_offload_nvidia(Path::new("/sys/class/drm"), &dirs)?;
    let value = keep.iter().map(|p| p.to_string_lossy().into_owned()).collect::<Vec<_>>().join(":");
    // SAFETY: called first thing in main, before any other thread exists.
    unsafe { std::env::set_var(ICD_ENV, &value) };
    Some(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    /// (card, driver, [(output, status)])
    type Card<'a> = (&'a str, &'a str, &'a [(&'a str, &'a str)]);

    fn drm(cards: &[Card]) -> tempfile::TempDir {
        let t = tempfile::tempdir().unwrap();
        for (card, driver, outputs) in cards {
            let dev = t.path().join(format!("devices/{card}"));
            std::fs::create_dir_all(dev.join("drivers")).unwrap();
            std::fs::create_dir_all(dev.join(format!("../{driver}"))).unwrap();
            std::fs::create_dir_all(t.path().join(card)).unwrap();
            symlink(dev.join(format!("../{driver}")), dev.join("driver")).unwrap();
            symlink(&dev, t.path().join(card).join("device")).unwrap();
            for (out, status) in *outputs {
                let o = t.path().join(format!("{card}-{out}"));
                std::fs::create_dir_all(&o).unwrap();
                std::fs::write(o.join("status"), format!("{status}\n")).unwrap();
            }
        }
        t
    }

    fn icd_dir() -> tempfile::TempDir {
        let t = tempfile::tempdir().unwrap();
        for f in ["intel_icd.x86_64.json", "nvidia_icd.json", "lvp_icd.x86_64.json", "README"] {
            std::fs::write(t.path().join(f), "{}").unwrap();
        }
        t
    }

    #[test]
    fn hybrid_laptop_on_the_integrated_gpu_drops_nvidia() {
        let d =
            drm(&[("card1", "i915", &[("eDP-1", "connected"), ("DP-1", "disconnected")]), ("card2", "nvidia", &[])]);
        let icds = icd_dir();
        let keep = icds_without_offload_nvidia(d.path(), &[icds.path().to_path_buf()]).unwrap();
        let names: Vec<String> = keep.iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();
        assert_eq!(names, ["intel_icd.x86_64.json", "lvp_icd.x86_64.json"]);
    }

    #[test]
    fn nvidia_driving_a_display_or_alone_is_left_alone() {
        let icds = icd_dir();
        let dirs = [icds.path().to_path_buf()];
        let d = drm(&[("card1", "i915", &[("eDP-1", "connected")]), ("card2", "nvidia", &[("HDMI-A-1", "connected")])]);
        assert_eq!(icds_without_offload_nvidia(d.path(), &dirs), None);
        let d = drm(&[("card0", "nvidia", &[("DP-1", "connected")])]);
        assert_eq!(icds_without_offload_nvidia(d.path(), &dirs), None);
        let d = drm(&[("card0", "amdgpu", &[("DP-1", "connected")])]);
        assert_eq!(icds_without_offload_nvidia(d.path(), &dirs), None, "no NVIDIA: nothing to do");
    }
}
