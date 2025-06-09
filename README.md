# AI-Enhanced EC2 Alarm Remediation with AWS Bedrock

Modern cloud environments generate **a high volume of alarms** (e.g. EC2 CPU or memory spikes) that can overwhelm on-call engineers. By integrating **Amazon Bedrock (Anthropic Claude v2)** into a Lambda-based workflow, you can automate data collection, AI-powered analysis, and remediation—all delivered to Slack and email without manual intervention. This guide walks you through deploying a serverless solution via the AWS Console.

> **AI-Powered Benefit:** Engineers receive immediate, natural-language explanations of root causes plus precise corrective steps—no more guessing or digging through raw metrics.

---

## 1. Create the Lambda Execution Role

1. In the AWS **IAM console**, choose **Roles ▶ Create role**.
2. For **Trusted Entity**, select **Lambda**.
3. Attach these managed policies (or equivalent custom policies):

   * **AWSLambdaBasicExecutionRole** (CloudWatch Logs)
   * **AmazonSSMFullAccess** (run commands)
   * **AmazonEC2ReadOnlyAccess** (describe instances)
   * **AmazonSNSFullAccess** (publish/subscribe)
   * **AmazonBedrockFullAccess** (invoke Claude v2)
4. Name it (e.g. `Lambda–Bedrock–Remediation–Role`) and note its ARN.

> Granting **BedrockFullAccess** lets your function call Claude v2 for instant AI insights.

---

## 2. Configure CloudWatch Alarms

### 2.1 CPU Utilization Alarm

1. In **CloudWatch ▶ Alarms**, click **Create alarm**.
2. Select your EC2 instance’s **CPUUtilization** metric; set threshold (e.g. > 80% for 5 minutes).
3. Under **Actions**, choose **Notify SNS topic** and select (or create) `AlarmTopic`.
4. Create the alarm.

### 2.2 Memory Utilization Alarm

EC2 doesn’t emit memory metrics by default—you must install the CloudWatch Agent:

1. Ensure the instance has the SSM Agent and an IAM instance profile (e.g. **AmazonSSMManagedInstanceCore**).
2. In **Systems Manager ▶ Run command**, use **AWS-ConfigureCloudWatch** to install and configure the CloudWatch Agent, selecting memory metrics.
3. After metrics flow into CloudWatch (namespace `CWAgent`), create an alarm on **mem\_used\_percent** (e.g. ≥ 90% for 3 periods) and point it to the **same SNS topic** (`AlarmTopic`).

> Memory visibility is crucial for diagnosing high-memory processes.

---

## 3. Create and Configure the SNS Topic

1. In **SNS ▶ Topics**, click **Create topic**; name it `AlarmTopic`.
2. Open the topic and under **Subscriptions** choose **Create subscription**:

   * **Protocol:** AWS Lambda
   * **Endpoint:** Your Lambda function (select later if needed)
3. (Optional) Add an **Email** subscription to `AlarmTopic` to receive the final remediation email.

> All alarms now funnel through SNS → Lambda, forming an automated pipeline.

---

## 4. Set Up Slack Incoming Webhook

1. In Slack, create an **App ▶ Incoming Webhooks**.
2. Enable a webhook for your target channel and copy the **Webhook URL**.
3. In your Lambda’s **Configuration ▶ Environment variables**, add:

   ```
   SLACK_WEBHOOK_URL = https://hooks.slack.com/services/…
   ```

> Storing the URL as an environment variable keeps your code portable and secure.

---

## 5. Deploy the Lambda Function

1. In **Lambda ▶ Functions**, click **Create function**.

   * **Name:** `EC2AlarmRemediation`
   * **Runtime:** Python 3.x
   * **Execution role:** `Lambda–Bedrock–Remediation–Role`
2. Under **Configuration ▶ Environment variables**, ensure you’ve set:

   * `SLACK_WEBHOOK_URL`
   * `SNS_TOPIC_ARN = arn:aws:sns:…:AlarmTopic`
3. Paste or upload the provided Python code, which will:

   1. **Parse the SNS message** to extract alarm details (instance ID, metric type, timestamp).
   2. **Run SSM commands** on the instance to list the top CPU or memory processes.
   3. **Invoke Bedrock (Claude v2)** with that data, requesting:

      * A plain-English **advice** paragraph
      * A JSON **action plan** of SSM commands + justifications
   4. **Execute** any safe commands immediately (e.g. `pkill`), deferring reboots until after notifications.
   5. **Post a single Slack message** (nine fields) with alarm context, AI advice, and actions taken.
   6. **Publish one final plaintext SNS message** (tagged `source=remediation`) so email subscribers receive exactly one complete email.
4. Increase function **timeout** (e.g. to 2 minutes) and **memory** (e.g. 512 MB).
5. Save and **Deploy**.

> **Pro Tip:** Enable CloudWatch Logs and sprinkle `print()` or `logging` statements at each step for easy debugging.

---

## 6. Test the End-to-End Flow

1. **Trigger an alarm:** Generate high CPU on your EC2 (e.g. stress test), or use CloudWatch’s **Test alarm** feature.
2. **Check Lambda logs:** In CloudWatch Logs, confirm the function ran successfully, collected data, and invoked Bedrock.
3. **Verify notifications:**

   * Slack channel should receive a structured JSON message with advice and actions.
   * Your email inbox (via SNS) should receive one clear, AI-driven remediation email.
4. **Troubleshoot:** If something fails, verify:

   * IAM permissions (SSM, EC2, SNS, Bedrock).
   * SSM Agent and CloudWatch Agent are running on the instance.
   * Environment variables are set correctly.

> **Result:** Each alarm now triggers an AI-powered diagnostic report and remediation sequence, reducing MTTR and eliminating manual guesswork.

---

## High-Level Workflow

1. **Alarm → SNS → Lambda**
2. **SSM Data Collection:** Top CPU/memory processes
3. **AI Analysis:** Claude v2 generates advice + JSON plan
4. **Remediation:** Commands executed automatically (reboots deferred)
5. **Notifications:** One Slack post + one email (no duplicates)

> This fully serverless solution brings generative AI directly into your incident response, **no infrastructure to maintain**, and scales effortlessly with every new alarm.
