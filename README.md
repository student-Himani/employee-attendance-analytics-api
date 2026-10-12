# Employee Attendance & Analytics API

## Project Status

**Status: Implemented and Tested**

The Employee Attendance & Analytics API has been implemented and verified through automated tests.

- **Automated tests:** 94 passed, 0 failed
- **API startup:** Verified successfully using Uvicorn
- **API documentation:** Swagger UI and OpenAPI schema accessible
- **Database connectivity:** MongoDB connection verified successfully
- **Health endpoint:** `/health` returned HTTP 200

### Technology Stack

- Python
- FastAPI
- MongoDB
- PyMongo
- OpenAPI 3.0.3
- Unit testing with Python `unittest`

### Run Locally

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Ensure MongoDB is running and configure the database connection as required by the application (defaults to `mongodb://localhost:27017` and database `attendance_db`).

3. (Optional) Seed sample records:

   ```bash
   python sample_seed.py
   ```

4. Start the API:

   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. Open the following URLs in your browser:

   - API documentation: `http://127.0.0.1:8000/docs`
   - Health check: `http://127.0.0.1:8000/health`

### Run Tests

```bash
python -m unittest tests/test_starter.py
```

The verified test run completed with **94 tests passing, 0 failing**.
