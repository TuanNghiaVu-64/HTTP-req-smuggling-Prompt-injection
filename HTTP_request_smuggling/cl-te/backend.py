#!/usr/bin/env python3
"""
CL.TE Backend Server
====================

This backend determines request boundaries using Transfer-Encoding: chunked.
It IGNORES Content-Length when Transfer-Encoding is present.

This is the "TE" half of CL.TE:
    Frontend = CL (Content-Length)
    Backend  = TE (Transfer-Encoding)

The desynchronization occurs because:
    - The frontend reads Content-Length bytes and forwards them all to the backend.
    - The backend reads using chunked encoding, which may consume FEWER bytes
      than what the frontend forwarded.
    - Any leftover bytes remain in the TCP stream and are parsed as a NEW request.

This is an intentionally vulnerable educational server. Do NOT use in production.
"""

import os
import socket
import threading
import sys
import time

# ─── Configuration ───────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 9000))

# ─── HTTP Response Templates ────────────────────────────────────────────────

RESPONSES = {
    ("GET", "/"): ("200 OK", "text/plain", "Hello world!\n"),
    ("GET", "/hello"): ("200 OK", "text/plain", "Hello from CL.TE backend!\n"),
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
        # Echo endpoint: return the body the backend received
        return build_response("200 OK", "text/plain", f"Echo: {body.decode('utf-8', errors='replace')}\n")

    if key in RESPONSES:
        status, ctype, body_text = RESPONSES[key]
        return build_response(status, ctype, body_text)

    # Unknown route
    return build_response("404 Not Found", "text/plain", f"404 Not Found: {method} {path}\n")


# ─── HTTP Parsing Utilities ─────────────────────────────────────────────────

def recv_until(sock: socket.socket, delimiter: bytes, buffer: bytearray) -> bytes:
    """
    Read from socket until `delimiter` is found in the accumulated buffer.
    Returns everything up to and including the delimiter.
    Remaining bytes stay in `buffer`.
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

    Returns:
        (method, path, version, headers_dict)

    Headers dict keys are lowercased for case-insensitive lookup.
    Values preserve original casing.
    """
    lines = header_block.decode("utf-8", errors="replace").split("\r\n")

    # Request line: METHOD SP PATH SP HTTP/VERSION
    request_line = lines[0]
    parts = request_line.split(" ", 2)
    if len(parts) < 3:
        raise ValueError(f"Malformed request line: {request_line!r}")

    method, path, version = parts

    # Parse headers (case-insensitive keys)
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


def read_chunked_body(sock: socket.socket, buffer: bytearray) -> bytes:
    """
    Read an HTTP/1.1 chunked transfer-encoded body.

    Chunked format:
        <chunk-size-hex>\r\n
        <chunk-data>\r\n
        ...
        0\r\n
        \r\n

    Returns the decoded body (without chunk framing).

    This is the critical parser for the CL.TE backend.
    The backend uses THIS to determine where the request body ends.
    """
    body = bytearray()

    while True:
        # Read chunk size line: "<hex-size>\r\n"
        size_line = recv_until(sock, b"\r\n", buffer)
        # size_line includes the trailing \r\n
        size_str = size_line.strip().decode("utf-8", errors="replace")

        # Handle optional chunk extensions (after semicolon)
        if ";" in size_str:
            size_str = size_str.split(";")[0].strip()

        try:
            chunk_size = int(size_str, 16)
        except ValueError:
            print(f"  [BACKEND] ERROR: Invalid chunk size: {size_str!r}", flush=True)
            break

        if chunk_size == 0:
            # Terminal chunk: read the trailing \r\n after "0\r\n"
            # This consumes the final "\r\n" that ends the chunked body.
            recv_until(sock, b"\r\n", buffer)
            break

        # Read exactly chunk_size bytes of data
        chunk_data = recv_exact(sock, chunk_size, buffer)
        body.extend(chunk_data)

        # Read the trailing \r\n after the chunk data
        recv_until(sock, b"\r\n", buffer)

    return bytes(body)


# ─── Connection Handler ─────────────────────────────────────────────────────

def handle_connection(client_sock: socket.socket, addr: tuple):
    """
    Handle a persistent TCP connection from the CL.TE frontend.

    CRITICAL BEHAVIOR:
    This backend uses Transfer-Encoding: chunked to determine request boundaries.
    After processing one request's chunked body, any remaining bytes in the TCP
    stream are treated as the START of a new HTTP request.

    This is what creates the CL.TE desynchronization:
    - Frontend sent N bytes (based on Content-Length).
    - Backend consumed M bytes (based on chunked encoding, where M <= N).
    - Remaining N - M bytes become a "smuggled" request.
    """
    print(f"\n[BACKEND] New connection from {addr}", flush=True)

    buffer = bytearray()
    request_num = 0

    try:
        while True:
            # ── Step 1: Read HTTP request headers ──
            # Headers end at \r\n\r\n
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
            # BACKEND RULE: If Transfer-Encoding: chunked is present, use it.
            #               Ignore Content-Length entirely.
            te = headers.get("transfer-encoding", "").lower()
            cl = headers.get("content-length", "")

            body = b""
            parser_mode = "NONE"

            if "chunked" in te:
                # ── CHUNKED MODE ──
                # This is the key behavior: the backend reads the body using
                # chunked encoding. It will stop at the "0\r\n\r\n" terminator,
                # potentially leaving bytes in the stream that the frontend
                # included (because the frontend used Content-Length).
                parser_mode = "TE (Transfer-Encoding: chunked)"
                body = read_chunked_body(client_sock, buffer)
            elif cl:
                # Fallback: if no TE header, use Content-Length
                parser_mode = "CL (Content-Length)"
                content_length = int(cl)
                if content_length > 0:
                    body = recv_exact(client_sock, content_length, buffer)
            else:
                parser_mode = "NONE (no body)"

            # ── Step 4: Log everything for educational visibility ──
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

            # ── Step 6: Check for more data (smuggled requests) ──
            # If there are remaining bytes in the buffer, the loop will
            # continue and parse them as a new HTTP request.
            # This is the smuggling mechanism!
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

    print(f"[CL.TE BACKEND] Listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[CL.TE BACKEND] Parser mode: Transfer-Encoding (TE)", flush=True)
    print(f"[CL.TE BACKEND] This backend uses chunked encoding to determine request boundaries.", flush=True)

    while True:
        client_sock, addr = server_sock.accept()
        thread = threading.Thread(target=handle_connection, args=(client_sock, addr), daemon=True)
        thread.start()


if __name__ == "__main__":
    main()
