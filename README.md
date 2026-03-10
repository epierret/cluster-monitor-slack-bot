# cluster-monitor-slack-bot
Lightweight monitoring tool that checks a Kubernetes cluster and Linux system state and sends alerts to Slack.  The bot runs as a systemd service, ensuring automatic startup and resilience.



How It Works

The monitoring bot runs continuously on a Linux host and performs checks such as:

Kubernetes node status

Pod failures

System resource usage

Cluster events

When an issue is detected, the bot sends a message to Slack.


Linux Host
   │
   │ systemd service
   ▼
Python Monitoring Bot
   │
   ├─ kubectl / Kubernetes API
   ├─ Linux system checks
   │
   ▼
Slack API
   │
   ▼
Alert in Slack Channel



Running the Service

Enable and start the service:

sudo systemctl daemon-reload
sudo systemctl enable k8s-slack-bot
sudo systemctl start k8s-slack-bot

Screenshots

Docker Check 

Kubernetes Check

<img width="517" height="117" alt="image" src="https://github.com/user-attachments/assets/d4b69daa-1820-4f23-9cf9-10bcf38a8720" />

System Info

<img width="435" height="332" alt="image" src="https://github.com/user-attachments/assets/d1b1767e-c925-438c-aca1-6bcb7192afb6" />

Future Improvements

precise wording to create ressources on cluster

Prometheus integration

Grafana alerts

Kubernetes API client instead of kubectl

Containerized version

Helm deployment






