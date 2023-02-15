import time
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import urllib.request,json

import boto3

#hostName = "ec2-XX-XX-XX-XX.compute-1.amazonaws.com"
hostName = "<your_host>"
serverPort = 9977
bucket = "<your_bucket>"
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
    webServer = HTTPServer((hostName, serverPort), MyServer)
    print("Server started http://%s:%s" % (hostName, serverPort))

    try:
        webServer.serve_forever()
    except KeyboardInterrupt:
        pass

    webServer.server_close()
    print("Server stopped.")
