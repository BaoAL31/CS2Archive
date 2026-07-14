#!/usr/bin/env python3
"""Proxy to see what pi's OpenAI SDK actually sends to NVIDIA."""
import http.server
import json
import urllib.request

TARGET = "https://integrate.api.nvidia.com"

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        
        print("=" * 60)
        print(f"PATH: {self.path}")
        print(f"HEADERS: {dict(self.headers)}")
        print(f"BODY: {body.decode('utf-8')}")
        
        # Forward to NVIDIA
        req = urllib.request.Request(
            f"{TARGET}{self.path}",
            data=body,
            headers={
                k: v for k, v in self.headers.items()
                if k.lower() not in ('host', 'content-length', 'transfer-encoding')
            },
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_body)
                if resp.status == 200:
                    print(f"RESPONSE: {resp.status} OK")
                else:
                    print(f"RESPONSE: {resp.status} {resp_body.decode()[:500]}")
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(err_body)
            print(f"RESPONSE: {e.code} {err_body.decode()[:500]}")
        except Exception as e:
            print(f"PROXY ERROR: {e}")
            self.send_response(502)
            self.end_headers()
        print("=" * 60)
    
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"proxy running")
    
    def log_message(self, format, *args):
        pass  # suppress default logging

if __name__ == '__main__':
    server = http.server.HTTPServer(('127.0.0.1', 9999), ProxyHandler)
    print("Proxy on http://127.0.0.1:9999 -> https://integrate.api.nvidia.com")
    server.serve_forever()
