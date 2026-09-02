# THSR legacy reference

This directory preserves unconnected THSR captcha-model and booking-flow source files for
historical reference. They are not part of the `tra-sniper` Python package, Docker image,
runtime, tests, or lint target.

The model consumes fixed-size character-captcha images from `irs.thsrc.com.tw`. It is not a
TRA model and is not suitable for TRA reCAPTCHA. Do not connect these files to the TRA booking
flow or describe them as TRA automation.

The supported TRA image feature lives in `src/tra_sniper/tra_ocr.py`. It recognizes ordinary
user-supplied ticket, booking-result, and timetable screenshots, then extracts text fields for
manual review and copying. It does not process CAPTCHA or reCAPTCHA challenges.
