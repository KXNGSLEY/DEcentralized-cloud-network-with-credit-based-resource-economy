# DEcentralized-cloud-network-with-credit-based-resource-economy
this project is unfinished and in development.....i will finish it.....one day
u can help if u like lol
# How to use
## to get hosts
curl "https://abf9ad30-1e7b-4a9b-b33b-686bafc19ab5-00-11oi3jcdhbhgk.spock.replit.dev/hosts"

to register as host
curl -X POST "https://abf9ad30-1e7b-4a9b-b33b-686bafc19ab5-00-11oi3jcdhbhgk.spock.replit.dev/register_host" -H "Content-Type: application/json" -d "{\"host\":\"my-host-01\"}"

or

curl -X POST "https://abf9ad30-1e7b-4a9b-b33b-686bafc19ab5-00-11oi3jcdhbhgk.spock.replit.dev/register_host" -H "Content-Type: application/json" -d "{\"host\":\"nonsking2215\"}"


to host jobs
curl -X POST "https://abf9ad30-1e7b-4a9b-b33b-686bafc19ab5-00-11oi3jcdhbhgk.spock.replit.dev/submit" -H "Content-Type: application/json" --data "@payload.json"
