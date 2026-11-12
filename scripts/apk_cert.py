#!/usr/bin/env python3
"""apk_cert - print the manager signature a KernelSU-family kernel matches for an APK.

KernelSU, KernelSU-Next and FinaSU recognise their manager by the first certificate of the
first signer in the APK Signature Scheme v2 block (kernel/manager/apk_sign.c, check_block()):
its length and its SHA-256 must equal a row compiled into kernelsu.ko. FinaSU's official
builds put the release key in the second slot (KSU_EXPECTED_SIZE2 / KSU_EXPECTED_HASH2);
build-lkm.yml uses this tool on the official manager APK to do the same.

Usage: apk_cert.py <apk> [<apk> ...]
Prints "<size as 0x%04x> <sha256>" for the certificate; with several APKs all must carry the
same certificate. Exit status 1 on any parse error or mismatch. Standard library only.
"""
import hashlib
import struct
import sys

EOCD_MAGIC = 0x06054B50
SIG_BLOCK_MAGIC = b"APK Sig Block 42"
V2_BLOCK_ID = 0x7109871A
CERT_MAX_LENGTH = 1024  # apk_sign.c rejects longer certificates


def v2_certificate(data):
    """DER bytes of the first certificate of the first v2 signer."""
    # End of central directory: the record whose comment length reaches the end of the file.
    for i in range(0x10000):
        p = len(data) - 22 - i
        if p < 0:
            break
        if struct.unpack_from("<I", data, p)[0] == EOCD_MAGIC and struct.unpack_from("<H", data, p + 20)[0] == i:
            eocd = p
            break
    else:
        raise ValueError("no ZIP end of central directory")
    cd_size, cd_off = struct.unpack_from("<II", data, eocd + 12)
    if cd_off + cd_size != eocd or cd_off < 32:
        raise ValueError("bad central directory offset")
    if data[cd_off - 16:cd_off] != SIG_BLOCK_MAGIC:
        raise ValueError("no APK Signing Block (the APK is not v2-signed)")
    size = struct.unpack_from("<Q", data, cd_off - 24)[0]
    start = cd_off - size - 8
    if start < 0 or struct.unpack_from("<Q", data, start)[0] != size:
        raise ValueError("APK Signing Block sizes do not match")

    pos, pairs_end, v2 = start + 8, cd_off - 24, []
    while pos < pairs_end:
        n = struct.unpack_from("<Q", data, pos)[0]
        if n < 4 or pos + 8 + n > pairs_end:
            raise ValueError("malformed APK Signing Block pair")
        if struct.unpack_from("<I", data, pos + 8)[0] == V2_BLOCK_ID:
            v2.append((pos + 12, pos + 8 + n))
        pos += 8 + n
    if len(v2) != 1:
        raise ValueError(f"expected one v2 signature block, found {len(v2)}")

    def prefixed(p, end):
        """(start, end) of the uint32-length-prefixed field at p."""
        if p + 4 > end:
            raise ValueError("truncated v2 block")
        n = struct.unpack_from("<I", data, p)[0]
        if p + 4 + n > end:
            raise ValueError("truncated v2 block")
        return p + 4, p + 4 + n

    block_start, block_end = v2[0]
    signers, signers_end = prefixed(block_start, block_end)  # sequence of signers
    signer, signer_end = prefixed(signers, signers_end)       # first signer
    sdata, sdata_end = prefixed(signer, signer_end)           # its signed data
    _, digests_end = prefixed(sdata, sdata_end)               # digests (skipped)
    certs, certs_end = prefixed(digests_end, sdata_end)       # sequence of certificates
    cert, cert_end = prefixed(certs, certs_end)               # first certificate
    if cert_end - cert > CERT_MAX_LENGTH:
        raise ValueError(f"certificate is {cert_end - cert} bytes; the kernel accepts at most {CERT_MAX_LENGTH}")
    return data[cert:cert_end]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.split("\n\n")[2])
    sigs = {}
    for path in sys.argv[1:]:
        try:
            with open(path, "rb") as f:
                cert = v2_certificate(f.read())
        except (OSError, ValueError, struct.error) as e:
            sys.exit(f"{path}: {e}")
        sig = f"0x{len(cert):04x} {hashlib.sha256(cert).hexdigest()}"
        print(f"{path}: {sig}", file=sys.stderr)
        sigs.setdefault(sig, []).append(path)
    if len(sigs) != 1:
        sys.exit("the APKs are signed with different certificates: " + "; ".join(
            f"{s} <- {', '.join(p)}" for s, p in sigs.items()))
    print(next(iter(sigs)))


if __name__ == "__main__":
    main()
