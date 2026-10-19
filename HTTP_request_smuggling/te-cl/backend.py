#!/usr/bin/env python3
"""
TE.CL Backend Server
====================

This backend determines request boundaries using Content-Length.
It IGNORES Transfer-Encoding when determining how many bytes to read.

This is the "CL" half of TE.CL:
    Frontend = TE (Transfer-Encoding)
    Backend  = CL (Content-Length)

The desynchronization occurs because:
    - The frontend decodes chunked encoding and forwards the decoded body.
    - The backend reads Content-Length bytes.
    - If Content-Length is smaller than the actual decoded body, the backend
      reads fewer bytes and treats the rest as a new HTTP request.

This is an intentionally vulnerable educational server. Do NOT use in production.
"""

import os
import socket
import threading
import sys
import time

# ─── Configuration ───────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 9001))

# ─── HTTP Response Templates ────────────────────────────────────────────────

RESPONSES = {
    ("GET", "/"): ("200 OK", "text/plain", "Hello world!\n"),
    ("GET", "/hello"): ("200 OK", "text/plain", "Hello from TE.CL backend!\n"),
    ("GET", "/admin"): ("200 OK", "text/plain", "ADMIN AREA\n"),
    ("POST", "/"): ("200 OK", "text/plain", "POST received.\n"),
    ("POST", "/echo"): None,  # Special: echoes body back
}


def build_response(status: str, content_type: str, body: str) -> bytes:
    """Build a raw HTTP/1.1 response from components."""
    body_bytes = body.encode("utf-8")
    response = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    ).encode("utf-8") + body_bytes
    return response


def route_request(method: str, path: str, body: bytes) -> bytes:
    """Route a parsed request to the appropriate handler and return response bytes."""
    key = (method.upper(), path)

    if key == ("POST", "/echo"):
        return build_response("200 OK", "text/plain", f"Echo: {body.decode('utf-8', errors='replace')}\n")

    if key in RESPONSES:
        status, ctype, body_text = RESPONSES[key]
        return build_response(status, ctype, body_text)

    return build_response("404 Not Found", "text/plain", f"404 Not Found: {method} {path}\n")


# ─── HTTP Parsing Utilities ─────────────────────────────────────────────────

def recv_until(sock: socket.socket, delimiter: bytes, buffer: bytearray) -> bytes:
    """
    Read from socket until `delimiter` is found in the accumulated buffer.
    Returns everything up to and including the delimiter.
    """
    while delimiter not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Connection closed while waiting for delimiter")
        buffer.extend(chunk)

    idx = buffer.index(delimiter) + len(delimiter)
    result = bytes(buffer[:idx])
    del buffer[:idx]
    return result


def recv_exact(sock: socket.socket, n: int, buffer: bytearray) -> bytes:
    """
    Read exactly `n` bytes from the socket/buffer combination.
    """
    while len(buffer) < n:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Connection closed while reading exact bytes")
        buffer.extend(chunk)

    result = bytes(buffer[:n])
    del buffer[:n]
    return result


def parse_headers(header_block: bytes) -> tuple:
    """
    Parse the request line and headers from a raw header block.
    Returns (method, path, version, headers_dict, raw_headers_list)
    """
    lines = header_block.decode("utf-8", errors="replace").split("\r\n")
    request_line = lines[0]
    parts = request_line.split(" ", 2)
    if len(parts) < 3:
        raise ValueError(f"Malformed request line: {request_line!r}")

    method, path, version = parts

    headers = {}
    raw_headers = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            headers[key.lower()] = value
            raw_headers.append((key, value))

    return method, path, version, headers, raw_headers


# ─── Connection Handler ─────────────────────────────────────────────────────

def handle_connection(client_sock: socket.socket, addr: tuple):
    """
    Handle a persistent TCP connection from the TE.CL frontend.

    CRITICAL BEHAVIOR (CL backend):
    This backend uses Content-Length to determine request boundaries.
    It IGNORES Transfer-Encoding entirely.

    After reading Content-Length bytes, any remaining bytes in the TCP stream
    are treated as the START of a new HTTP request.

    This is what creates the TE.CL desynchronization:
    - Frontend decoded the chunked body and forwarded it.
    - Backend reads only Content-Length bytes.
    - Remaining bytes become a "smuggled" request.
    """
    print(f"\n[BACKEND] New connection from {addr}", flush=True)

    buffer = bytearray()
    request_num = 0

    try:
        while True:
            # ── Step 1: Read HTTP request headers ──
            try:
                header_block = recv_until(client_sock, b"\r\n\r\n", buffer)
            except ConnectionError:
                print(f"[BACKEND] Connection closed by frontend ({addr})", flush=True)
                break

            request_num += 1

            # ── Step 2: Parse the request line and headers ──
            try:
                method, path, version, headers, raw_headers = parse_headers(header_block)
            except ValueError as e:
                print(f"[BACKEND] Parse error: {e}", flush=True)
                error_resp = build_response("400 Bad Request", "text/plain", f"Parse error: {e}\n")
                client_sock.sendall(error_resp)
                continue

            # ── Step 3: Determine body parsing mode ──
            # BACKEND RULE: Use Content-Length ONLY. Ignore Transfer-Encoding.
            te_value = headers.get("transfer-encoding", "")
            cl_value = headers.get("content-length", "")

            body = b""
            parser_mode = "NONE"

            if cl_value:
                # ── CONTENT-LENGTH MODE ──
                # Read exactly Content-Length bytes.
                # If the frontend forwarded more data than CL indicates,
                # the extra bytes remain in the stream → smuggled request.
                parser_mode = "CL (Content-Length)"
                try:
                    content_length = int(cl_value)
                except ValueError:
                    content_length = 0
                if content_length > 0:
                    body = recv_exact(client_sock, content_length, buffer)
            else:
                parser_mode = "NONE (no Content-Length)"

            # ── Step 4: Log everything ──
            remaining_preview = bytes(buffer[:200]) if buffer else b""

            print(f"""
============================================================
BACKEND REQUEST #{request_num}
============================================================

Request line:
  {method} {path} {version}

Headers:""", flush=True)
            for k, v in raw_headers:
                print(f"  {k}: {v}", flush=True)

            print(f"""
Parser mode:
  {parser_mode}

Transfer-Encoding (IGNORED by this backend):
  {te_value if te_value else '(not present)'}

Content-Length used:
  {cl_value if cl_value else '(not present)'}

Body length:
  {len(body)} bytes

Body bytes (repr):
  {body!r}

Body text:
  {body.decode('utf-8', errors='replace')}

Raw request header bytes (repr):
  {header_block!r}

Remaining bytes in TCP stream buffer ({len(buffer)} bytes):
  {remaining_preview!r}

============================================================
""", flush=True)

            # ── Step 5: Route and respond ──
            response = route_request(method, path, body)
            client_sock.sendall(response)

            # ── Step 6: Check for smuggled requests ──
            if buffer:
                print(f"  [BACKEND] [WARNING] {len(buffer)} bytes remaining in stream — "
                      f"will attempt to parse as next request!", flush=True)

    except Exception as e:
        print(f"[BACKEND] Error handling connection from {addr}: {e}", flush=True)
    finally:
        client_sock.close()
        print(f"[BACKEND] Connection closed for {addr}", flush=True)


# ─── Main Server ────────────────────────────────────────────────────────────

def main():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((LISTEN_HOST, LISTEN_PORT))
    server_sock.listen(5)

    print(f"[TE.CL BACKEND] Listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[TE.CL BACKEND] Parser mode: Content-Length (CL)", flush=True)
    print(f"[TE.CL BACKEND] This backend uses Content-Length to determine request boundaries.", flush=True)
    print(f"[TE.CL BACKEND] Transfer-Encoding header is IGNORED.", flush=True)

    while True:
        client_sock, addr = server_sock.accept()
        thread = threading.Thread(target=handle_connection, args=(client_sock, addr), daemon=True)
        thread.start()


if __name__ == "__main__":
    main()
