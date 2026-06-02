import requests
r = requests.post("http://localhost:11434/api/generate", json={"model":"llama3","prompt":"test","stream":False})
print(r.status_code, r.json())
