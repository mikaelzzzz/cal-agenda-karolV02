# Cal.com → Notion + WhatsApp Integration

This service integrates Cal.com with Notion and WhatsApp (via Z-API) to:
1. Update Notion database when a booking is created/rescheduled
2. Send WhatsApp reminders to admins:
   - 1 day before the meeting
   - 4 hours before the meeting
   - 1 hour after the meeting

## Requirements

- Python 3.11+
- FastAPI
- Notion API access
- Z-API WhatsApp account
- Cal.com account with webhook capability

## Setup

1. Clone this repository
2. Create a virtual environment and activate it:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

## Environment Variables

- `CAL_SECRET`: Webhook secret configured in Cal.com
- `NOTION_TOKEN`: Notion API integration token
- `NOTION_DB`: Notion database ID where lead information is stored
- `ZAPI_INSTANCE`: Z-API instance ID
- `ZAPI_TOKEN`: Z-API instance token
- `ADMIN_PHONES`: Comma-separated list of phone numbers to receive notifications
- `TZ`: Timezone for date conversions (default: America/Sao_Paulo)
- `PORT`: Port for the FastAPI server (default: 8000)

## Running Locally

```bash
uvicorn main:app --reload
```

## Deploying to Render

1. Create a new Web Service in Render
2. Connect your repository
3. Configure the service:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add all environment variables from `.env` in the Render dashboard
5. Deploy!

## API Endpoints

- `GET /`: Health check endpoint
- `POST /webhook/cal`: Cal.com webhook endpoint

## Webhook Setup in Cal.com

1. Go to Cal.com webhook settings
2. Add a new webhook pointing to your deployed service: `https://your-service.onrender.com/webhook/cal`
3. Configure the webhook secret and add it to your environment variables
4. Select the events: `BOOKING_CREATED` and `BOOKING_RESCHEDULED`

## Notion Setup

1. Create a Notion integration: https://www.notion.so/my-integrations
2. Share your database with the integration
3. Add the following properties to your database:
   - `Email` (Email type)
   - `Telefone` (Phone type)
   - `Data Agendada pelo Lead` (Text type)

## Z-API Setup

1. Create a Z-API account and instance
2. Connect your WhatsApp number
3. Get your instance ID and token
4. Add them to your environment variables

## License

MIT 