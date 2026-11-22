//! Protecting the device key on disk.
//!
//! Two layers:
//!   * File permissions: C:\ProgramData\SMP and agent.json are readable only by
//!     SYSTEM (the service) and administrators. Students can't read the key.
//!   * DPAPI, machine scope: the key in agent.json is encrypted with a secret of this
//!     Windows installation. A copy of the file is useless on another computer (or
//!     after reinstalling Windows), so a cloned disk image can't impersonate this PC.
//!     Machine scope (not "SYSTEM only") because `smp-agent install` runs as the
//!     administrator who installs it, while the service later runs as SYSTEM.
//!
//! A later step can move the key into the TPM (P-256 is TPM-compatible).

use std::path::Path;

use anyhow::{bail, Context, Result};
use windows::core::HSTRING;
use windows::Win32::Foundation::{LocalFree, HLOCAL};
use windows::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1};
use windows::Win32::Security::Cryptography::{
    CryptProtectData, CryptUnprotectData, CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
};
use windows::Win32::Security::{SetFileSecurityW, DACL_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR};

use super::wide;

/// SDDL: "protected" (don't inherit from the parent folder); full access for SYSTEM (SY)
/// and Administrators (BA); OICI = folders pass it on to the files inside.
const DIR_ACL: &str = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)";
const FILE_ACL: &str = "D:P(A;;FA;;;SY)(A;;FA;;;BA)";

pub fn protect(secret: &[u8]) -> Result<(&'static str, Vec<u8>)> {
    let input = CRYPT_INTEGER_BLOB { cbData: secret.len() as u32, pbData: secret.as_ptr() as *mut u8 };
    let mut output = CRYPT_INTEGER_BLOB::default();
    // SAFETY: input points to our buffer; Windows allocates output, we free it below.
    unsafe {
        CryptProtectData(
            &input,
            &HSTRING::from("SMP agent key"),
            None,
            None,
            None,
            CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
        .context("DPAPI: encrypting the key")?;
        Ok(("dpapi-machine", take_blob(output)))
    }
}

pub fn unprotect(protection: &str, data: &[u8]) -> Result<Vec<u8>> {
    match protection {
        "dpapi-machine" => {
            let input = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
            let mut output = CRYPT_INTEGER_BLOB::default();
            // SAFETY: as in protect().
            unsafe {
                CryptUnprotectData(&input, None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, &mut output)
                    .context("DPAPI: this key was made on another computer or Windows installation")?;
                Ok(take_blob(output))
            }
        }
        other => bail!("unknown key protection {other:?}"),
    }
}

/// Copy a buffer Windows allocated for us, then free it.
unsafe fn take_blob(blob: CRYPT_INTEGER_BLOB) -> Vec<u8> {
    let bytes = std::slice::from_raw_parts(blob.pbData, blob.cbData as usize).to_vec();
    let _ = LocalFree(Some(HLOCAL(blob.pbData.cast())));
    bytes
}

/// Only SYSTEM and administrators may read `path` (and its folder).
pub fn restrict_file(path: &Path) -> Result<()> {
    if let Some(dir) = path.parent() {
        set_acl(dir, DIR_ACL)?;
    }
    set_acl(path, FILE_ACL)
}

fn set_acl(path: &Path, sddl: &str) -> Result<()> {
    let mut sd = PSECURITY_DESCRIPTOR::default();
    // SAFETY: Windows allocates `sd` from the SDDL text; we free it after use.
    unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(&HSTRING::from(sddl), SDDL_REVISION_1, &mut sd, None)
            .context("building the ACL")?;
        let result = SetFileSecurityW(&wide(path), DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, sd);
        let _ = LocalFree(Some(HLOCAL(sd.0)));
        result.ok().with_context(|| format!("setting permissions on {}", path.display()))
    }
}
