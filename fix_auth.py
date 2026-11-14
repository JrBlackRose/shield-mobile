with open("server.py", "r") as f:
    code = f.read()

old_line = 'AUTH_TOKEN = os.getenv("SHIELD_AUTH_TOKEN", "secure_shield_key_2026")'
new_code = 'from dotenv import load_dotenv\nload_dotenv()\nAUTH_TOKEN = os.getenv("SHIELD_AUTH_TOKEN", "secure_shield_key_2026").replace("\\"", "").replace("\\\'", "").strip()'

code = code.replace(old_line, new_code)

with open("server.py", "w") as f:
    f.write(code)

print("[+] Auth token sanitized.")
