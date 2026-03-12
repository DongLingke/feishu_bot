@echo off

where python >nul 2>nul
IF ERRORLEVEL 1 (
    echo python is required.
    EXIT /b 1
)

IF "%MOCK_DIFY_HOST%"=="" SET MOCK_DIFY_HOST=127.0.0.1
IF "%MOCK_DIFY_PORT%"=="" SET MOCK_DIFY_PORT=18080

python -m pip install -r requirements.txt
IF ERRORLEVEL 1 (
    EXIT /b 1
)

start "mock-dify" /B python mock_dify_server.py

IF "%DIFY_API_BASE_URL%"=="" SET DIFY_API_BASE_URL=http://%MOCK_DIFY_HOST%:%MOCK_DIFY_PORT%/v1
IF "%DIFY_WORKFLOW_PAGE_URL%"=="" SET DIFY_WORKFLOW_PAGE_URL=http://%MOCK_DIFY_HOST%:%MOCK_DIFY_PORT%/mock-workflow
IF "%DIFY_API_KEY%"=="" SET DIFY_API_KEY=local-test-key

echo mock dify is running at %DIFY_API_BASE_URL%
echo starting feishu bot...

python main.py
