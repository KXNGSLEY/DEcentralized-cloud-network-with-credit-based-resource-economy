# DEcentralized-cloud-network-with-credit-based-resource-economy
this is a personal project, it is unfinished and in development.....i will finish it.....one day lol....
u can help if u like
# How to use
## INSTRUCTIONS ON HOW TO USE 
## to Run the software(and initiate your system as the supernode)
python SUPER𝙉𝙀𝙏_V1.0.py

## to get hosts (nodes)
curl "http://YOUR_SERVER_IP:9000/hosts"

## to register as host (node)
curl -X POST "http://YOUR_SERVER_IP:9000/register_host" -H "Content-Type: application/json" -d "{\"host\":\"whatever-hostname-ya-want-lol\"}"

## to host jobs
curl -X POST "http://YOUR_SERVER_IP:9000/submit" -H "Content-Type: application/json" --data "@payload.json"

## to host job from github
curl -X POST "http://YOUR_SERVER_IP:9000/submit" -H "Content-Type: application/json" -d "{\"github_url\":\"https://github.com/username/repo\",\"entrypoint\":\"main.py\",\"user_id\":\"kxngsley\"}"


## to ping a host (node)
curl -X POST "https://YOUR_SERVER_IP:9000/host_ping" -H "Content-Type: application/json" -d "{\"host\":\"hostname\"}"
