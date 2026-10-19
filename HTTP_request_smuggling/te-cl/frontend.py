#!/usr/bin/env python3
"""
TE.CL Frontend Proxy
=====================

This frontend determines request boundaries using Transfer-Encoding: chunked.
It IGNORES Content-Length when determining the request boundary.

This is the "TE" half of TE.CL:
    Frontend = TE (Transfer-Encoding)
    Backend  = CL (Content-Length)

The desynchronization occurs because:
    - The frontend decodes the chunked body (stopping at 0\r\n\r\n).
    - It then re-forwards the decoded request to the backend.
    - The backend uses Content-Length to determine body length.
    - If the attacker embeds a small Content-Length value inside the forwarded
      body, the backend reads fewer bytes and treats the remainder as a new
      HTTP request.

IMPORTANT: This frontend DECODES chunked encoding and forwards the decoded
body to the backend with the Content-Length that was in the original request.
The backend then uses that Content-Length, which may be smaller than the
actual decoded body, causing desynchronization.

This is an intentionally vulnerable educational proxy. Do NOT use in production.
"""

import os
import socket
import threading
import sys
import time

# ─── Configuration ───────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 8081))

BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", 9001))

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


def parse_request_line_and_headers(header_block: bytes) -> tuple:
    """
    Parse the request line and headers from raw bytes.
    Returns (method, path, version, headers_dict, raw_header_lines)
    """
    text = header_block.decode("utf-8", errors="replace")
    if text.endswith("\r\n\r\n"):
        text = text[:-4]
    elif text.endswith("\r\n"):
        text = text[:-2]

    lines = text.split("\r\n")
    request_line = lines[0]
    parts = request_line.split(" ", 2)
    if len(parts) < 3:
        raise ValueError(f"Malformed request line: {request_line!r}")

    method, path, version = parts

    headers = {}
    raw_headers = []
    for line in lines[1:]:
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
    Returns the decoded body (without chunk framing).
    """
    body = bytearray()

    while True:
        size_line = recv_until(sock, b"\r\n", buffer)
        size_str = size_line.strip().decode("utf-8", errors="replace")

        if ";" in size_str:
            size_str = size_str.split(";")[0].strip()

        try:
            chunk_size = int(size_str, 16)
        except ValueError:
            print(f"  [FRONTEND] ERROR: Invalid chunk size: {size_str!r}", flush=True)
            break

        if chunk_size == 0:
            recv_until(sock, b"\r\n", buffer)
            break

        chunk_data = recv_exact(sock, chunk_size, buffer)
        body.extend(chunk_data)

        recv_until(sock, b"\r\n", buffer)

    return bytes(body)


# ─── Backend Connection & Response Streaming ───────────────────────────────

def connect_to_backend() -> socket.socket:
    """Create a persistent TCP connection to the backend."""
    max_retries = 10
    for attempt in range(max_retries):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((BACKEND_HOST, BACKEND_PORT))
            print(f"  [FRONTEND] Connected to backend {BACKEND_HOST}:{BACKEND_PORT}", flush=True)
            return sock
        except ConnectionRefusedError:
            print(f"  [FRONTEND] Backend not ready, retrying ({attempt+1}/{max_retries})...", flush=True)
            time.sleep(1)

    raise ConnectionError(f"Cannot connect to backend after {max_retries} attempts")


def forward_responses(backend_sock: socket.socket, client_sock: socket.socket):
    """
    Continuously stream responses from backend to client.
    This ensures that when a smuggled request produces an extra response
    from the backend, it is immediately forwarded to the client.
    """
    try:
        while True:
            chunk = backend_sock.recv(4096)
            if not chunk:
                break
            client_sock.sendall(chunk)
            print(f"  [FRONTEND] Forwarded {len(chunk)} bytes from backend to client", flush=True)
    except Exception:
        pass


# ─── Connection Handler ─────────────────────────────────────────────────────

def handle_client(client_sock: socket.socket, addr: tuple):
    """
    Handle a client TCP connection.

    CRITICAL BEHAVIOR (TE frontend):
    1. Read headers until \r\n\r\n.
    2. If Transfer-Encoding: chunked is present, decode the chunked body.
    3. Forward the request headers (with original Content-Length intact) + decoded body
       to the backend on a persistent TCP connection.
    4. The backend will use Content-Length to parse the body.
    5. If Content-Length < actual decoded body length, the backend treats
       the surplus bytes as a new HTTP request → smuggling!
    """
    print(f"\n[FRONTEND] New client connection from {addr}", flush=True)

    client_buffer = bytearray()
    backend_sock = None
    request_num = 0

    try:
        backend_sock = connect_to_backend()

        # Start response forwarding thread from backend to client
        resp_thread = threading.Thread(
            target=forward_responses,
            args=(backend_sock, client_sock),
            daemon=True
        )
        resp_thread.start()

        while True:
            # ── Step 1: Read request headers from client ──
            try:
                header_block = recv_until(client_sock, b"\r\n\r\n", client_buffer)
            except ConnectionError:
                print(f"[FRONTEND] Client disconnected ({addr})", flush=True)
                break

            request_num += 1

            # ── Step 2: Parse headers ──
            try:
                method, path, version, headers, raw_headers = parse_request_line_and_headers(header_block)
            except ValueError as e:
                print(f"[FRONTEND] Parse error: {e}", flush=True)
                error_resp = (
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Content-Length: 15\r\n"
                    b"\r\n"
                    b"400 Bad Request"
                )
                client_sock.sendall(error_resp)
                continue

            # ── Step 3: Determine body using TRANSFER-ENCODING ONLY ──
            te_value = headers.get("transfer-encoding", "").lower()
            cl_value = headers.get("content-length", "")

            body = b""
            parser_mode = "NONE"

            if "chunked" in te_value:
                # ── CHUNKED MODE ──
                # Frontend decodes the chunked body.
                # The decoded body is then forwarded to the backend.
                parser_mode = "TE (Transfer-Encoding: chunked)"
                body = read_chunked_body(client_sock, client_buffer)
            elif cl_value:
                # Fallback if no chunked encoding
                parser_mode = "CL (fallback)"
                try:
                    content_length = int(cl_value)
                except ValueError:
                    content_length = 0
                if content_length > 0:
                    body = recv_exact(client_sock, content_length, client_buffer)

            # ── Step 4: Rebuild and forward request to backend ──
            # CRITICAL: We forward the request headers as-is (including original
            # Content-Length) plus the DECODED body.
            # The backend will use Content-Length to parse this body.
            # If Content-Length < len(decoded body), desync occurs.
            forwarded = header_block + body
            total_bytes = len(forwarded)

            # ── Step 5: Log the frontend's interpretation ──
            print(f"""
============================================================
FRONTEND REQUEST #{request_num}
============================================================

Parser mode: {parser_mode}

Request line:
  {method} {path} {version}

Transfer-Encoding:
  {te_value if te_value else '(not present)'}

Content-Length (IGNORED by frontend for boundary, but forwarded):
  {cl_value if cl_value else '(not present)'}

Frontend decoded body length:
  {len(body)} bytes

Body bytes (repr):
  {body!r}

Total bytes forwarding to backend:
  {total_bytes} bytes

Forwarding to backend...
============================================================
""", flush=True)

            # ── Step 6: Forward to backend ──
            try:
                backend_sock.sendall(forwarded)
            except (BrokenPipeError, ConnectionError):
                print("  [FRONTEND] Backend connection lost, reconnecting...", flush=True)
                backend_sock.close()
                backend_sock = connect_to_backend()
                resp_thread = threading.Thread(
                    target=forward_responses,
                    args=(backend_sock, client_sock),
                    daemon=True
                )
                resp_thread.start()
                backend_sock.sendall(forwarded)

    except Exception as e:
        print(f"[FRONTEND] Error handling client {addr}: {e}", flush=True)
    finally:
        if backend_sock:
            backend_sock.close()
        client_sock.close()
        print(f"[FRONTEND] Connection closed for {addr}", flush=True)


# ─── Main Server ────────────────────────────────────────────────────────────

def main():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((LISTEN_HOST, LISTEN_PORT))
    server_sock.listen(5)

    print(f"[TE.CL FRONTEND] Listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[TE.CL FRONTEND] Parser mode: Transfer-Encoding (TE)", flush=True)
    print(f"[TE.CL FRONTEND] This frontend uses chunked encoding to determine request boundaries.", flush=True)
    print(f"[TE.CL FRONTEND] Content-Length is forwarded but NOT used for boundary detection.", flush=True)

    while True:
        client_sock, addr = server_sock.accept()
        thread = threading.Thread(target=handle_client, args=(client_sock, addr), daemon=True)
        thread.start()


if __name__ == "__main__":
    main()
