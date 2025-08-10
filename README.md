# DEcentralized-cloud-network-with-credit-based-resource-economy
this is a personal project, it is unfinished and in development.....i will finish it.....one day lol....
u can help if u like
# How to use
## INSTRUCTIONS ON HOW TO USE 
## to Run the software(and initiate your system as the supernode)
python supernet_server.py

## to get hosts (nodes)
curl "http://YOUR_SERVER_IP:9000/hosts"

## to register as host (node)
curl -X POST "http://YOUR_SERVER_IP:9000/register_host" -H "Content-Type: application/json" -d "{\"host\":\"whatever-hostname-ya-want-lol\"}"

## to host jobs
curl -X POST "http://YOUR_SERVER_IP:9000/submit" -H "Content-Type: application/json" --data "@payload.json"

## to ping a host (node)
curl -X POST "https://YOUR_SERVER_IP:9000/host_ping" -H "Content-Type: application/json" -d "{\"host\":\"hostname\"}"
