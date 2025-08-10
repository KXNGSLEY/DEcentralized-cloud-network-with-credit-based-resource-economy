# DEcentralized-cloud-network-with-credit-based-resource-economy
this project is unfinished and in development.....i will finish it.....one day
u can help if u like lol
# How to use
## INSTRUCTIONS ON HOW TO USE 
python supernet_server.py

## to get hosts
curl "http://YOUR_SERVER_IP:9000/hosts"

## to register as host
curl -X POST "http://YOUR_SERVER_IP:9000/register_host" -H "Content-Type: application/json" -d "{\"host\":\"whatever-hostname-ya-want-lol\"}"

##to host jobs
curl -X POST "http://YOUR_SERVER_IP:9000/submit" -H "Content-Type: application/json" --data "@payload.json"
