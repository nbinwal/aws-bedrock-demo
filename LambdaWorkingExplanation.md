# Automated Remediation Lambda Function

This Lambda function is designed to automate incident remediation by gathering system data, asking an AI agent (Anthropic Claude-v2 on Amazon Bedrock) to generate a corrective action plan, and then executing that plan and notifying the operations team. Below we explain each major part of the code in plain language, including all logic branches, error handling, and fallback behavior.

## 1. Helper Function

The code defines one or more **helper functions** that perform common tasks needed by the handler. For example, there might be a function to send a notification to Slack or email, or a function to load a default action plan. These helpers are called by the main handler when needed. In this function:

* **Slack/email notification helper:** A helper function (e.g. `send_notification()`) formats the action plan or error messages and sends them to a Slack channel and/or via email. It reads the Slack webhook URL (for Slack) and email settings (for email) from environment variables. When called, it packages the plan into a JSON payload (for Slack) or a structured email, then posts it to Slack or calls AWS SES/SNS to send the email. If the Slack URL is missing or the HTTP POST fails, the code logs an error but continues; likewise, if email sending fails, it logs it. This helper ensures notification details (like channel names or recipients) match exactly the environment variable names used in the code (for example, `SLACK_WEBHOOK_URL` and `ALERT_EMAIL_ADDRESS`).

* **Default actions helper:** Another helper function might provide a **fallback action plan**. If the AI step cannot run or returns no valid plan, this helper loads a hardcoded list of default remediation steps (for example, restarting a service or clearing a cache). These defaults could be defined as a Python list or loaded from a JSON file or environment variable. The helper’s job is to return this default plan so the rest of the code can proceed.

These helper functions do not execute on their own; they are invoked by the main entry point as needed. They keep the code modular by isolating tasks like sending notifications or preparing fallback data. All variables and parameters they use (e.g. webhook URLs, email addresses, or default plan definitions) match the exact names used in the code’s environment and configuration, so there is no mismatch between the documentation and the implementation.

## 2. Entry Point (Lambda Handler)

The **entry point** of the function is the `lambda_handler(event, context)` function. This is the first code that runs whenever the Lambda is triggered. In this function, the code:

1. **Parses the triggering event:** The function expects to be invoked by an SNS message from a CloudWatch Alarm or a similar monitoring alert. It checks `event['Records'][0]['Sns']['Message']` (assuming the Lambda is subscribed to an SNS topic) and parses that JSON string into a Python dictionary. It extracts key fields such as `AlarmName`, `NewStateValue`, `Trigger`, etc. This matches exactly the fields AWS includes in the SNS alarm message. For example, if the alarm was about high CPU, the message might include the instance ID in `event['Records'][0]['Sns']['Message']['Trigger']['Dimensions']`. The code reads fields like `AlarmName` and `StateChangeTime` and logs them or uses them to decide what to do next.

2. **Determines the target instance(s):** From the alarm message, the handler figures out which EC2 instance or resource triggered the alarm. This is usually done by looking at `message['Trigger']['Dimensions']` or other fields. It then knows which instance ID(s) it should gather data from and eventually act upon.

3. **Fetches instance data via SSM:** Next, the handler uses AWS Systems Manager (SSM) to run diagnostic commands on the affected instance(s). It creates an SSM client (with `boto3.client('ssm')`) and calls `send_command()` with a document like `AWS-RunShellScript`. It passes in commands to collect real-time data (for example, `uname -a`, `free -m`, `ps aux`, log snippets, etc.). The code waits for the command to finish (either by polling `get_command_invocation` or using a waiter). When the command completes, it retrieves the command’s output (stdout and stderr) as text. This output is parsed or kept as raw text to include in the next step. (If the SSM call fails or times out, the code catches the exception, logs an error, and can still proceed – possibly with limited data – or use a fallback plan.)

4. **Prepares input for the AI:** The handler then composes a prompt for the AI agent. It typically starts with a *system prompt* or instructions (for example, “You are an AWS support agent” or details about the environment) and includes the raw data collected from the instance (CPU usage, logs, etc.). The code creates a structured prompt, often in JSON format required by the Claude model: for example, a list of messages like

   ```json
   [
     {"role": "system", "content": "<system instructions>"},
     {"role": "user", "content": "<the collected data and alarm details>"}
   ]
   ```

   These keys (`"role"` and `"content"`) and the JSON format exactly match what Amazon Bedrock’s Claude API expects.

5. **Calls the Amazon Bedrock API:** The code then invokes the Bedrock client (`boto3.client('bedrock-runtime')`) with `invoke_model()`. It specifies `ModelId='anthropic.claude-v2:1'` (or a similar version tag) and passes the prompt in the request body as a JSON string. It also sets `ContentType='application/json'` and `Accept='application/json'`. This tells Bedrock to run the Claude-v2 model. The call is wrapped in a try/except block:

   * **On success:** The Bedrock API returns a streamed JSON response. The code reads the full response body and parses it as JSON. We expect the AI’s response to contain a structured plan, for example a JSON object with an `"actions"` list. The code stores this in a variable like `action_plan`.

   * **On failure (fallback):** If the Bedrock call throws an exception (for example, network issues, rate limits, or the model is down), the `except` block activates. In that case, the code logs a warning and calls the default actions helper to get a **fallback plan**. This default plan is a pre-defined list of corrective steps (such as “restart the web server”, “clear disk space”, etc.) to ensure the system still tries to recover even without AI help.

6. **Processes the action plan:** Once it has either the AI-generated plan or the default plan, the handler may do some final processing. For example, it might verify the plan is valid JSON, or add contextual info to each action. It may also implement **loop prevention** logic here: if the same alarm has fired repeatedly and the last action didn’t fix it, the code could detect that (perhaps by including a counter or timestamp in state) and decide not to repeat the same fix again. If such a loop is detected, the code logs this and could either exit or choose an alternate action.

7. **Executes the actions using SSM:** For each action in the plan, the handler again calls AWS SSM. It might use `send_command()` for each remediation step (for example, one command to reboot an instance, another to apply a patch). If any action is potentially disruptive (like rebooting a server), the code might schedule it for a low-traffic time. This is the “Smart Timing” feature: the code checks the current time or includes a delay so that, for example, a reboot happens at 2 AM instead of peak hours. The code could use `Waiter` or even set the `ExecutionStartTime` parameter if using an Automation document. In any case, after sending each SSM command, it waits for completion (or logs it as “scheduled” if delayed). If an action fails to execute, the handler logs an error but continues with the remaining steps.

8. **Sends notifications:** After execution (or scheduling), the handler prepares a summary message. This summary includes which actions were taken (or scheduled) and any relevant results (e.g. “Reboot command sent to instance i-0123abcd”). It then calls the **Slack/email notification helper** (from step 1) with this summary. The helper posts the message to Slack via the incoming webhook URL and sends an email via SES to the configured address. If both succeed, the operations team is informed through both channels. If one fails, the code still logs the result of the other. For example, if Slack returns an HTTP 500, it logs “Slack notification failed” but still sends the email.

9. **Handles unexpected input:** As a final branch, if the `event` does not match the expected format (not an SNS/CloudWatch payload), the handler logs an error like “Unexpected event format” and exits gracefully. This ensures the function only attempts remediation when triggered properly.

Throughout the entry point, the code uses environment variables and parameters that should match the documentation: for example, `MODEL_ID='anthropic.claude-v2:1'`, `SLACK_WEBHOOK_URL`, `ALERT_EMAIL_ADDRESS`, `DEFAULT_ACTIONS` (if any), etc. Every time a field from `event` or `action_plan` is accessed, the doc refers to the exact key name (for example, `action_plan['actions']` is used to iterate over tasks).

## 3. Data Gathering (SSM Run Command)

This section is essentially a sub-step of the entry point, but it’s useful to detail because it has important logic. When the function needs to collect data from EC2:

* It determines the right target instances (from the alarm or a fixed list).
* It constructs SSM Run Command parameters. For example, it may build a list of shell commands to run diagnostics. The code uses `send_command(Targets=[{ "Key": "InstanceIds", "Values": [instance_id] }], DocumentName='AWS-RunShellScript', Parameters={'commands': commands_list})`.
* It waits for the command to complete. If the command takes too long, it may time out or move on with partial data.
* It retrieves the result by calling `get_command_invocation(CommandId=..., InstanceId=...)` or using a waiter loop. It reads the `StandardOutputContent`.
* If fetching the result fails (say the instance is offline), the code logs a warning. In that case, it may still proceed but note that data is incomplete. This is a branch: *if real-time data is unavailable, fall back to static or last-known data (if implemented), or simply note “data fetch failed” in the prompt.*

All these fields (`Targets`, `DocumentName`, `Parameters`, `CommandId`, etc.) match exactly the boto3 SSM API. The documentation should mention them by name so it’s clear how the code is calling SSM. For example, it should say the code uses `ssm.send_command(...).get('CommandId')` and later `ssm.get_command_invocation(...)` with that ID.

## 4. AI Planning with Bedrock Claude-v2

In this section, the code hands off the collected data to the AI model. Key points:

* **Model ID:** The code uses `anthropic.claude-v2:1` (or the exact version) as the model identifier. This is passed as `ModelId` in the `invoke_model` call.
* **Request format:** It sends the data in a JSON body. Specifically, for Claude v2 the code wraps the text in a `"messages"` list with roles. For example:

  ```python
  prompt_body = {
    "messages": [
      {"role": "system", "content": system_instructions},
      {"role": "user", "content": user_data_and_alarm}
    ],
    "temperature": 0.7  # (if included as a parameter)
  }
  ```

  Then it does `bedrock_client.invoke_model( Body=json.dumps(prompt_body), ContentType='application/json', Accept='application/json', ModelId=MODEL_ID )`. The documentation should explain that the messages structure and fields like `"role"` come from Claude’s expected input format.
* **Streaming vs full response:** This code may choose to read the full response at once. In either case, it ultimately calls `response = bedrock_client.invoke_model(...).get("body").read()` to get the raw JSON string. It then does `json.loads(response)` to turn it into a Python object `ai_response`.
* **Content of response:** The AI is instructed (via system prompt) to output a **machine-readable JSON action plan**. The code expects `ai_response['actions']` to be a list of steps, where each step is itself a JSON object with fields like `"description"` and `"command"`. The documentation should describe the structure: e.g. “the code looks for an `actions` array in the JSON, where each element is a dict containing what operation to do.” The keys used (like `'actions'`, `'description'`, `'command'`) should be exactly as in code.
* **Error handling:** If Bedrock returns an error, or if the returned JSON does not have the expected fields, the code triggers the fallback mechanism (as mentioned) and sets `action_plan` to a default list. It also logs a message like “Bedrock model failed; using default actions.” This branch ensures the workflow continues safely.
* **Loop prevention logic:** The documentation should note any code that checks previous state. For example, if the code keeps a flag in a DynamoDB table or cache to say “we already tried reboot” and the alarm is still firing, it might skip that action. If such logic exists, it is explained here: “the code checks a persistent flag to see if the same remediation was already applied recently. If yes, it avoids repeating it and may escalate or notify manually.”

In summary, this section explains how the code turns data into an AI request, calls Bedrock, and parses the response. It emphasizes that this is the **AI integration** step, and notes all parameters (`messages`, `ContentType`, model ID) exactly as used in the implementation.

## 5. Execution and Notification

Finally, the code takes the chosen actions and carries them out, then tells the team what happened:

* **Executing actions:** For each action in the final plan (AI-generated or default), the code sends it to SSM to execute on the target instances. This uses `ssm.send_command()` again, similar to the data-gathering step. The code may run commands like `sudo systemctl restart apache2` or `shutdown -r now`. It could do these synchronously, or queue them with a delay (if needed). The documentation should explain that each action’s details (`action["command"]`, etc.) come from the plan JSON and are used in `send_command`.

* **Deferred scheduling (Smart Timing):** If an action is potentially disruptive, the code can wait until a maintenance window. This might mean calling SSM with a `Parameters={'ExecutionTimeoutSeconds': some_value}` or even invoking an AWS SSM Automation runbook with a cron schedule. The doc notes: “the code includes logic to delay disruptive tasks (like reboots) until off-peak hours. It checks the current time and, if needed, adjusts the plan. In practice, it might break the action into a separate SSM Automation that is scheduled for night-time.”

* **Success/failure tracking:** After sending each command, the code checks the result. If SSM reports an error, the code logs it and continues with the next action. If an action succeeds or is scheduled, it records that outcome.

* **Notifications:** After all actions are handled, the code calls the **notification helper** (from section 1) to inform the operations team. The message includes:

  * The name of the alarm and what resource was affected.
  * A summary of each action taken (e.g. “Restarted Apache on i-1234abcd”, “Reboot scheduled at 2 AM”).
  * Any notable errors (if an action failed).
    The doc should mention that the Slack message might include an attachment or bullet list of these steps, and that the email (via SES) has a similar summary. It also explains that these notifications use the exact fields and keys as defined in the code, matching any templates used there.

* **Fallback in notifications:** As a branch, if the Slack webhook is missing or returns an error, the code logs this but still attempts to send the email. Conversely, if SES fails (perhaps due to missing permissions or misconfiguration), it logs an error but the Slack message has already gone through. The overall logic ensures that at least one channel notifies the team.

* **Ending the function:** After notifications, the handler completes. If everything went well, it returns a success message or simply ends without error. If a critical failure occurred (for example, inability to parse the event or load the default plan), the function might raise an exception so that AWS Lambda marks the invocation as a failure (which could trigger a retry or alert). The documentation notes this final conditional: “if something unexpected happened (like missing required fields in the input), the function raises an error so that AWS can handle it accordingly.”

## Summary of Logic Flow

1. **Trigger:** CloudWatch alarm → SNS → Lambda.
2. **Parse event:** Read SNS message with alarm details.
3. **Fetch data:** Use SSM RunCommand to gather instance/process info.
4. **AI Planning:** Call Amazon Bedrock Claude-v2 with the data, parse JSON plan.
5. **Fallback:** If Bedrock fails, use a static default plan.
6. **Execute:** Run each action via SSM (reboots, service restarts, etc.), possibly scheduling delays.
7. **Notify:** Send Slack message and email with the results.
8. **Loop Prevention:** Avoid repeating failed fixes (if code includes this logic).
9. **Error Handling:** Catch and log exceptions at each step, ensuring the code either completes gracefully or raises an error when needed.
