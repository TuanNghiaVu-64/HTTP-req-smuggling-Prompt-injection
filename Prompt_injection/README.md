# Indirect Prompt Injection Lab

A beginner-friendly, local educational cybersecurity lab demonstrating **indirect prompt injection** vulnerabilities in LLM-powered applications.

Inspired by the conceptual model of PortSwigger's indirect prompt injection research, this lab shows how untrusted external data (such as product reviews or comments) can hijack an AI model's reasoning and control its output.

The complete code, templates, and styles are located in the [`prompt-injection-lab/`](./prompt-injection-lab) folder.

---

## Quick Start (Windows)

### 1. Activate Environment
```powershell
.\.venv\Scripts\Activate.ps1
```

### 2. Install Dependencies
```powershell
pip install -r prompt-injection-lab/requirements.txt
```

### 3. Ensure Ollama is Running with Default Model
```powershell
ollama serve
ollama pull llama3.2:3b
```

### 4. Run the Lab
```powershell
cd prompt-injection-lab
python app.py
```
Open **[http://127.0.0.1:5000](http://127.0.0.1:5000)** in your browser.

---

## Complete Documentation
For full walkthroughs, architecture explanations, testing guides, and troubleshooting, see the [prompt-injection-lab/README.md](./prompt-injection-lab/README.md).
