# AI-Enhanced EC2 Alarm Remediation with AWS Bedrock

Modern cloud environments generate **a high volume of alarms** (e.g. EC2 CPU or memory spikes) that can overwhelm engineers. Using **Amazon Bedrock (Anthropic Claude v2)**, we can automate the analysis of alarm context and produce clear remediation advice. This guide shows how to deploy a Lambda-based solution via the AWS console that uses Bedrock to simplify troubleshooting. When an EC2 alarm fires, the Lambda function runs commands on the instance via AWS Systems Manager (SSM) to list the top CPU/memory processes, sends that data to Claude for analysis, and then publishes the AI-generated guidance to an email (via SNS) and to Slack (via webhook). The result is concise, actionable advice delivered automatically to your team. This is a fully managed, serverless approach: **no infrastructure to maintain** in order to integrate advanced generative AI into your operations.

> **AI-Powered Benefit:** By leveraging a Bedrock foundation model, cloud engineers get instant, natural-language explanations of complex alarms. This augments human expertise and scales effectively to handle many alerts simultaneously.

## 1. Create the Lambda IAM Role

1. In the AWS **IAM console**, choose **Roles ▶ Create role**. Select **Lambda** as the trusted entity.
2. Attach the following managed policies (you may also create a custom policy bundle):

   * **AWSLambdaBasicExecutionRole** (for CloudWatch Logs)

   * **AmazonSSMFullAccess** (to run SSM commands on instances)

   * **AmazonEC2ReadOnlyAccess** (to describe EC2 instances)

   * **AmazonSNSFullAccess** (to publish messages and subscribe to SNS)

   * **AmazonBedrockFullAccess** (or AmazonBedrockReadOnly) (to invoke Bedrock models).

   > **Tip:** Amazon Bedrock is a fully managed, serverless service providing high-performance AI models. Granting *BedrockFullAccess* lets the Lambda call Claude v2 for analysis.
3. Name the role (e.g. `Lambda-Bedrock-Remediation-Role`) and create it. The role’s ARN will be used when creating the function.

## 2. Configure CloudWatch Alarms (CPU and Memory)

### 2.1 CPU Utilization Alarm

* Open the **CloudWatch console ▶ Alarms ▶ All alarms**, then choose **Create alarm**.
* Select the EC2 instance’s **CPUUtilization** metric. Define a threshold (e.g. >80% for 5 minutes) and choose **Next**.
* Under **Actions**, select **Notify a topic**, then choose *AlarmTopic* (an SNS topic) or create one. This SNS topic will trigger the Lambda. Finally **Create alarm**.

### 2.2 Memory Utilization Alarm

EC2 does not emit memory metrics by default. You must install the CloudWatch Agent to collect memory data:

1. **Install/Configure CloudWatch Agent on EC2**:

   * Ensure the instance has the SSM Agent running and an IAM instance profile (e.g. **AmazonSSMManagedInstanceCore**) so it’s managed by SSM.
   * In the **CloudWatch console ▶ Agent status**, or via **Systems Manager Run Command**, install the CloudWatch Agent (AWS provides an SSM document **AWS-ConfigureCloudWatch**).
   * Configure the agent to collect memory metrics (e.g. run the `amazon-cloudwatch-agent-config-wizard` on the instance, selecting memory and CPU). This publishes memory metrics (in the `CWAgent` namespace) to CloudWatch.
   * For detailed steps, see AWS guidance: “You can setup memory metrics by installing and configuring CloudWatch agent on each EC2 instance.”.

2. **Create Memory Alarm**:

   * After the agent sends memory data (e.g. *mem\_used\_percent*), in CloudWatch create an alarm on that metric (for example, Memory % >= 90% for several periods).
   * As with CPU, configure the alarm to **Notify the same SNS topic**. Now both high CPU and high memory alarms publish to SNS.

> **Note:** Installing the CloudWatch agent gives you visibility into memory usage. This extra context is crucial for troubleshooting heavy-memory processes. AWS recommends customizing the agent to collect just the metrics you need, then alarms can be defined on those.

## 3. Create and Configure the SNS Topic

1. In the **SNS console ▶ Topics**, choose **Create topic**. Give it a name (e.g. `AlarmTopic`) and display name.
2. **Subscribe the Lambda to the topic**:

   * After creating the topic, open it and under **Subscriptions** choose **Create subscription**.
   * For **Protocol**, select **AWS Lambda**, and for **Endpoint**, pick the Lambda function you will create (or choose later). This ensures that whenever the alarm publishes to this topic, the Lambda is invoked with the alarm message.
3. **Subscribe an email endpoint** for receiving the remediation advice:

   * If you want immediate email notifications, create another SNS topic (e.g. `AdviceTopic`) or reuse the same topic with a filter, and subscribe your operations email (choose **Protocol = Email**). Confirm the subscription via the email link.
   * The Lambda will later publish its analysis to this topic to send out the advice.

> **AI-Powered Benefit:** By channeling alarms through SNS and Lambda, you create an automated pipeline. Each alarm’s details (instance ID, CPU/memory usage, etc.) become the input to the AI analysis, which generates context-rich guidance without manual intervention.

## 4. Set Up Slack Webhook

1. In your Slack workspace, create a **Slack App** (via *api.slack.com/apps*), add the **Incoming Webhooks** feature, and activate a webhook for the desired channel. Copy the generated **Webhook URL**.
2. In the **Lambda function configuration** (next step), you will set an environment variable, e.g. `SLACK_WEBHOOK_URL`, to this URL. This lets the function send POST requests to Slack.

   * To add this in the console: go to the Lambda’s **Configuration ▶ Environment variables**, click **Edit**, then **Add environment variable**. For example:

   ```
   SLACK_WEBHOOK_URL = https://hooks.slack.com/services/T0000/B0000/XXXX
   ```

> **AI-Powered Insight:** Having the Slack webhook as an environment variable allows the same function code to target any channel dynamically. The AI-driven advice will flow directly into chat alongside email, keeping your team informed wherever they collaborate.

## 5. Create and Deploy the Lambda Function

1. In the **Lambda console ▶ Functions**, choose **Create function**. Name it (e.g. `EC2AlarmRemediation`), select the Python runtime, and set **Execution role** to the IAM role created earlier.
2. Under **Configuration ▶ Environment variables**, ensure `SLACK_WEBHOOK_URL` (and any SNS topic ARN if needed) are set as above.
3. Deploy the function code. Use the provided code template (or write your own) that does the following steps:

   * **Parse the SNS message** (JSON) to extract the EC2 instance ID and alarm details.

   * **Use boto3 SSM** to run a command on the instance (e.g. `aws ssm send_command` with document `AWS-RunShellScript`) that returns the top processes by CPU or memory (for example, `ps aux --sort=-%mem | head -n 10`).

   * **Retrieve the command output** using `get_command_invocation` (poll until complete). Collect the text of top processes.

   * **Call Amazon Bedrock** (`invoke_model` or `invoke_model_with_response_stream` API) using Claude v2, sending a system/user message like:

     > *“The following processes on EC2 instance i-012345 show high CPU and memory usage (details below). What might be causing the resource spike, and what steps should be taken to resolve it?”*
     > Include the command output in the prompt.

   * **Receive Claude’s response**, which should be a concise analysis and remediation steps.

   * **Publish the advice**: use boto3 SNS to publish the response text to the *AdviceTopic* (so it goes to your email list).

   * **Post to Slack**: use a Python HTTP library (e.g. `requests`) to `POST` the response text to the `SLACK_WEBHOOK_URL`.
4. Adjust function settings: increase timeout (e.g. 1–2 minutes) to allow SSM and Bedrock calls. Increase memory if needed.
5. Save and test. You can create a test event that mimics the SNS alarm message JSON or simply publish a test message to the SNS topic to trigger the function.

> **Pro Tip:** Include logging at each step (parsing SNS, SSM invocation, Bedrock input/output) for easier troubleshooting. Verify IAM permissions if the function fails to call SSM or Bedrock.

## 6. Test the End-to-End Flow

* **Simulate an Alarm:** Generate high CPU on your EC2 instance (e.g. run a CPU loop script) or use the **CloudWatch “Test alarm”** feature. The alarm should go into ALARM state and publish to SNS.
* **Check Lambda Execution:** In CloudWatch Logs, view the Lambda logs to ensure it ran successfully and see the SSM and Bedrock outputs.
* **Verify Email:** You should receive an email (via SNS) with a clear, AI-generated explanation of the problem and suggested fixes.
* **Verify Slack:** Check the designated Slack channel – the Lambda should have posted the same advice via the webhook.
* **Troubleshoot:** If something went wrong, check that the IAM role has correct permissions (especially SSM and Bedrock), that SSM Agent is running on the EC2, and that the CloudWatch Agent is sending memory metrics if testing memory alarms.

> **Result:** You now have a live system where CloudWatch alarms automatically trigger an LLM-powered analysis, turning raw metric data into human-friendly action items. This greatly accelerates incident response and reduces noise.

## Lambda Function Features (High-Level Workflow)

* **Automated Trigger:** The Lambda function is invoked by the SNS notification from a CloudWatch alarm. It doesn’t require manual action to start analysis.
* **Context Gathering via SSM:** It runs predefined shell commands on the affected EC2 instance (through AWS Systems Manager) to fetch the **top resource-consuming processes**. This gives concrete context (e.g. which application or service is causing high load).
* **AI Analysis with Bedrock (Claude v2):** The function sends the process list and alarm details to Amazon Bedrock’s Claude model. Claude quickly **interprets the data**, identifies likely causes (e.g. “Process X might be stuck in a loop”) and recommends specific remediation steps (e.g. “Kill the runaway process and check its logs.”).
* **Multi-Channel Notification:** The AI-generated advice is then **pushed out to teams**: it’s sent as an email (via SNS) and also posted to Slack. Engineers see a clear summary of the issue and next steps without digging through raw metrics.
* **No Manual Diagnosis Needed:** By leveraging Bedrock, the solution converts technical data into plain-English explanations, saving time. Instead of each alarm being a “mystery,” the function provides a ready-made report with possible fixes.

> **AI-Powered Insight:** This Lambda is essentially an automated diagnostics assistant. It scales to handle many simultaneous alarms and provides standardized, easy-to-understand guidance. In high-volume environments, this means faster incident resolution and less strain on engineering teams.
