Set-Location (Split-Path $PSScriptRoot -Parent)
python -m streamlit run ui/streamlit_app.py --server.address 127.0.0.1 --server.port 8501
