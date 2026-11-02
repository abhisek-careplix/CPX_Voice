# careplix-openemr: the IAP-protected front door to the hosted OpenEMR demo (Cloud Run).
# IAP signs staff in with their CarePlix Google account; nginx forwards to the private VM.
FROM nginx:1.27-alpine
COPY default.conf.template /etc/nginx/templates/default.conf.template
ENV PORT=8080
