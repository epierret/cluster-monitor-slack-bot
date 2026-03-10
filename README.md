

Here's a Lightweight monitoring tool that checks Docker , Kubernetes cluster and Linux system state and sends alerts to Slack.

The bot runs as a systemd service, ensuring:

automatic startup at boot

resilience through automatic restart

continuous monitoring of the host and cluster

This project demonstrates Linux service management, Kubernetes interaction, and operational alerting.

Architecture


                +-----------------------+
                |     Slack Channel     |
                |  (alerts & messages)  |
                +-----------▲-----------+
                            |
                            | Slack API
                            |
                 +----------+----------+
                 |   Python Monitoring |
                 |         Bot         |
                 +----------▲----------+
                            |
          +-----------------+-----------------+
          |                                   |
          |                                   |
   +------▼-------+                    +------▼------+
   |  Linux Host  |                    | Kubernetes  |
   |              |                    |   Cluster   |
   | systemd      |                    |             |
   | service      |                    | nodes/pods  |
   +--------------+                    +-------------+


   How It Works

The monitoring bot runs continuously on a Linux host and performs several checks:

* Kubernetes checks

* Node status

* Pod failures

* Cluster events

* Pod availability

* Linux system checks

* CPU usage

* Memory usage

* Disk usage

*Docker status

When an issue is detected, the bot sends a Slack alert to notify operators.

Service Architecture

The monitoring workflow is the following:
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

Check the service status:
systemctl status k8s-slack-bot



Screenshots
Kubernetes Check
<img width="517" height="117" alt="kubernetes-check" src="https://github.com/user-attachments/assets/d4b69daa-1820-4f23-9cf9-10bcf38a8720" />


System Info
<img width="435" height="332" alt="system-info" src="https://github.com/user-attachments/assets/d1b1767e-c925-438c-aca1-6bcb7192afb6" />


Technologies Used

* Python

* Kubernetes

* kubectl

* Slack API

* Linux

* systemd


Future Improvements

This project is designed as an experimental monitoring tool and will evolve over time.

Planned improvements include:

* More precise wording for cluster resource creation via Slack

*Remote administration through FastAPI or Claude Desktop with an MCP server

* RAG integration for troubleshooting and operational support

* Prometheus metrics integration

* Grafana alerting dashboards

* Replace kubectl calls with the Kubernetes Python client

* Containerized deployment

* Helm chart deployment


Purpose of the Project

* This repository is part of a DevOps learning portfolio focused on:

* Linux service management with systemd

* Kubernetes monitoring automation

* Slack operational alerting

* Building lightweight operational tooling

The project is intentionally iterative and may evolve as new monitoring or automation ideas are explored.
