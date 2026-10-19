#!/usr/bin/env python3
"""
Raw TCP Testing Client
======================

Sends exact byte sequences to the HTTP smuggling lab servers without any
HTTP library normalization. This is essential because tools like curl or
requests may alter headers, normalize line endings, or reject malformed
requests — defeating the purpose of the lab.

Usage:
    python tests/raw_client.py --host 127.0.0.1 --port 8080 --file examples/cl-te-request.txt
    python tests/raw_client.py --host 127.0.0.1 --port 8080 --payload "POST / HTTP/1.1\r\n..."
    python tests/raw_client.py --host 127.0.0.1 --port 8080 --file examples/cl-te-request.txt --hex

This client:
    1. Opens a raw TCP socket.
    2. Sends the exact file/payload bytes (no modifications).
    3. Keeps the connection open to receive responses.
    4. Prints raw response bytes.
    5. Optionally shows hex dump for debugging.
"""

import socket
import argparse
import sys
import time


def hex_dump(data: bytes, width: int = 16) -> str:
    """Format bytes as a hex dump with ASCII sidebar."""
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {offset:08x}  {hex_part:<{width*3}}  |{ascii_part}|")
    return "\n".join(lines)


def escaped_repr(data: bytes) -> str:
    """Show bytes with visible escape sequences."""
    return repr(data).lstrip("b").strip("'\"")


def send_raw_request(host: str, port: int, payload: bytes,
                     recv_timeout: float = 3.0, show_hex: bool = False,
                     keep_open: float = 2.0):
    """
    Send raw bytes over TCP and print responses.

    Args:
        host: Target host
        port: Target port
        payload: Raw bytes to send
        recv_timeout: Timeout for receiving responses
        show_hex: Show hex dump of response
        keep_open: How long to keep connection open after last data
    """
    print(f"{'='*60}")
    print(f"RAW TCP CLIENT")
    print(f"{'='*60}")
    print(f"Target: {host}:{port}")
    print(f"Payload size: {len(payload)} bytes")
    print()

    if show_hex:
        print("Payload hex dump:")
        print(hex_dump(payload))
        print()

    print(f"Payload (escaped):")
    print(f"  {escaped_repr(payload)}")
    print()

    # Connect
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(recv_timeout)

    try:
        sock.connect((host, port))
        print(f"Connected to {host}:{port}")
        print()

        # Send exact payload bytes
        sock.sendall(payload)
        print(f"Sent {len(payload)} bytes")
        print()

        # Receive responses
        print(f"{'='*60}")
        print(f"RESPONSES")
        print(f"{'='*60}")
        print()

        all_response_data = b""
        response_num = 0

        # Keep reading until timeout (connection stays open for smuggled responses)
        deadline = time.time() + keep_open + recv_timeout
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    print("[Connection closed by server]")
                    break
                all_response_data += chunk
                response_num += 1

                print(f"--- Response chunk #{response_num} ({len(chunk)} bytes) ---")
                print(chunk.decode("utf-8", errors="replace"))

                if show_hex:
                    print("Hex:")
                    print(hex_dump(chunk))
                    print()

                # Reset deadline on new data
                deadline = time.time() + keep_open

            except socket.timeout:
                if all_response_data:
                    break
                continue

        print(f"{'='*60}")
        print(f"SUMMARY")
        print(f"{'='*60}")
        print(f"Total response data: {len(all_response_data)} bytes")
        print(f"Response chunks received: {response_num}")

        if show_hex and all_response_data:
            print()
            print("Full response hex dump:")
            print(hex_dump(all_response_data))

    except ConnectionRefusedError:
        print(f"ERROR: Connection refused to {host}:{port}")
        print("Make sure the lab containers are running: docker compose up --build")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        sock.close()
        print()
        print("[Connection closed]")


def load_payload_file(filepath: str) -> bytes:
    """
    Load a payload file as raw bytes.
    Normalizes line endings to \r\n for HTTP compliance.
    """
    with open(filepath, "rb") as f:
        data = f.read()

    # Normalize line endings: \r\n is required for HTTP
    # Replace \r\n first to avoid double-conversion, then \n → \r\n
    data = data.replace(b"\r\n", b"\n")  # Normalize to \n
    data = data.replace(b"\n", b"\r\n")  # Convert to \r\n

    return data


def main():
    parser = argparse.ArgumentParser(
        description="Raw TCP client for HTTP Request Smuggling lab testing"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Target host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, required=True,
                        help="Target port (8080 for CL.TE, 8081 for TE.CL)")
    parser.add_argument("--file", "-f",
                        help="Path to file containing raw HTTP request payload")
    parser.add_argument("--payload", "-p",
                        help="Raw payload string (use \\r\\n for line endings)")
    parser.add_argument("--hex", action="store_true",
                        help="Show hex dumps of payload and responses")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="Socket receive timeout in seconds (default: 3.0)")
    parser.add_argument("--keep-open", type=float, default=2.0,
                        help="Keep connection open N seconds after last data (default: 2.0)")

    args = parser.parse_args()

    if args.file:
        payload = load_payload_file(args.file)
    elif args.payload:
        # Process escape sequences in the payload string
        payload = args.payload.encode("utf-8").decode("unicode_escape").encode("latin-1")
    else:
        parser.error("Either --file or --payload is required")
        return

    send_raw_request(
        host=args.host,
        port=args.port,
        payload=payload,
        recv_timeout=args.timeout,
        show_hex=args.hex,
        keep_open=args.keep_open,
    )


if __name__ == "__main__":
    main()
