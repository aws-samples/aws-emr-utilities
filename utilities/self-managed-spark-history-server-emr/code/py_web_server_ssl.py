import time
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import urllib.request,json
import ssl
import boto3

# You can use this command to generate self-signed cert for testing and replace in line 28: openssl req -newkey rsa:2048 -nodes -keyout key.pem -x509 -days 365 -out cert.pem
#hostName = "ec2-XX-XX-XX-XX.compute-1.amazonaws.com"
hostName = "0.0.0.0"
serverPort = 9977
bucket = "<your bucket>"
client = boto3.client('s3')

class MyServer(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path).path
        print(parsed_path[1:])
        self.send_response(302)
        resource = str(parsed_path[1:])
        url = client.generate_presigned_url('get_object', Params = {'Bucket': bucket, 'Key': resource}, ExpiresIn = 100)
        self.send_header('Location', url)
        self.end_headers()

if __name__ == "__main__":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile='<REPLACE YOUR CERT.PEM HERE>', keyfile='<REPLACE YOUR KEY.PEM HERE>')
    webServer = HTTPServer((hostName, serverPort), MyServer)
    webServer.socket = context.wrap_socket(webServer.socket, server_side=True)
    print("Server started https://%s:%s" % (hostName, serverPort))

    try:
        webServer.serve_forever()
    except KeyboardInterrupt:
        pass

    webServer.server_close()
    print("Server stopped.")
