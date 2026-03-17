mkdir -p ~/.streamlit

cat > ~/.streamlit/config.toml << EOF
[server]
headless = true
enableCORS = false
enableXsrfProtection = false

[theme]
base = "light"
primaryColor = "#d97706"
backgroundColor = "#ffffff"
secondaryBackgroundColor = "#f8fafc"
textColor = "#0f172a"
font = "monospace"
EOF
