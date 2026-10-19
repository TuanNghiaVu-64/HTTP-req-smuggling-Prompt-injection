#!/usr/bin/env python3
"""
CL.TE Frontend Proxy
=====================

This frontend determines request boundaries using Content-Length.
It IGNORES Transfer-Encoding when determining how many bytes to read.

This is the "CL" half of CL.TE:
    Frontend = CL (Content-Length)
    Backend  = TE (Transfer-Encoding)

The desynchronization occurs because:
    - The frontend reads exactly Content-Length bytes as the body.
    - It forwards the ENTIRE raw request (headers + body) to the backend.
    - The backend then re-parses using Transfer-Encoding: chunked.
    - If the chunked body terminates early (e.g., "0\r\n\r\n" appears before
      Content-Length bytes are exhausted), the remaining bytes sit in the TCP
      stream and the backend treats them as a new HTTP request.

This is an intentionally vulnerable educational proxy. Do NOT use in production.
"""

import os
import socket
import threading
import sys
import time

# ─── Configuration ───────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 8080))

BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", 9000))

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


def parse_request_line_and_headers(header_block: bytes) -> tuple:
    """
    Parse the request line and headers from raw bytes.
    Returns (method, path, version, headers_dict, raw_header_lines)
    """
    text = header_block.decode("utf-8", errors="replace")
    # Remove trailing \r\n\r\n
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


# ─── Backend Connection & Response Streaming ───────────────────────────────

def connect_to_backend() -> socket.socket:
    """
    Create a persistent TCP connection to the backend.
    This connection will be reused for multiple forwarded requests.
    """
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

    CRITICAL BEHAVIOR (CL frontend):
    1. Read headers until \r\n\r\n.
    2. Find Content-Length header.
    3. Read EXACTLY Content-Length bytes as the body.
    4. Forward the complete raw request (headers + body) to the backend.
    5. Transfer-Encoding header is IGNORED for boundary determination.
    6. Keep the backend connection persistent across multiple client requests.
    """
    print(f"\n[FRONTEND] New client connection from {addr}", flush=True)

    client_buffer = bytearray()
    backend_sock = None
    request_num = 0

    try:
        # Establish persistent backend connection
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

            # ── Step 3: Determine body length using CONTENT-LENGTH ONLY ──
            # This is the critical CL frontend behavior:
            # We ONLY look at Content-Length. We do NOT process Transfer-Encoding.
            # Even if Transfer-Encoding: chunked is present, we ignore it.
            cl_value = headers.get("content-length", "0")
            try:
                content_length = int(cl_value)
            except ValueError:
                content_length = 0

            te_value = headers.get("transfer-encoding", "")

            # ── Step 4: Read exactly Content-Length bytes as body ──
            body = b""
            if content_length > 0:
                body = recv_exact(client_sock, content_length, client_buffer)

            # The total raw request = headers + body
            total_forwarded = header_block + body
            total_bytes = len(total_forwarded)

            # ── Step 5: Log the frontend's interpretation ──
            print(f"""
============================================================
FRONTEND REQUEST #{request_num}
============================================================

Parser mode: CL (Content-Length)

Request line:
  {method} {path} {version}

Content-Length:
  {cl_value}

Transfer-Encoding (IGNORED by frontend):
  {te_value if te_value else '(not present)'}

Frontend believes request body is:
  {content_length} bytes

Total bytes consumed (headers + body):
  {total_bytes} bytes

Body bytes (repr):
  {body!r}

Forwarding {total_bytes} bytes to backend...
============================================================
""", flush=True)

            # ── Step 6: Forward raw bytes to backend ──
            try:
                backend_sock.sendall(total_forwarded)
            except (BrokenPipeError, ConnectionError):
                print("  [FRONTEND] Backend connection lost, reconnecting...", flush=True)
                backend_sock.close()
                backend_sock = connect_to_backend()
                # Restart response forwarder for new connection
                resp_thread = threading.Thread(
                    target=forward_responses,
                    args=(backend_sock, client_sock),
                    daemon=True
                )
                resp_thread.start()
                backend_sock.sendall(total_forwarded)

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

    print(f"[CL.TE FRONTEND] Listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[CL.TE FRONTEND] Parser mode: Content-Length (CL)", flush=True)
    print(f"[CL.TE FRONTEND] This frontend uses Content-Length to determine request boundaries.", flush=True)
    print(f"[CL.TE FRONTEND] Transfer-Encoding header is forwarded but NOT used for parsing.", flush=True)

    while True:
        client_sock, addr = server_sock.accept()
        thread = threading.Thread(target=handle_client, args=(client_sock, addr), daemon=True)
        thread.start()


if __name__ == "__main__":
    main()
