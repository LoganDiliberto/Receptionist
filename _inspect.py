"""Verify salon.py loads + tool calls work end-to-end."""
import asyncio
from salon import check_availability, book_appointment, system_prompt_context, INFO

print("=== Salon static info (system prompt block) ===")
print(system_prompt_context())
print()

async def main():
    print("=== check_availability for tomorrow (any stylist, any service) ===")
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = await check_availability(tomorrow)
    print(r)

    print(f"\n=== check_availability for {tomorrow} (Mandy, color) ===")
    r = await check_availability(tomorrow, stylist="Mandy", service="color")
    print(r)

    print(f"\n=== book_appointment: cut with Joey at 1:00 PM on {tomorrow} ===")
    r = await book_appointment("Test Caller", "+15551234", "Joey", "cut", tomorrow, "13:00")
    print(r)

    print(f"\n=== try to double-book Joey at 1:00 PM ===")
    r = await book_appointment("Another Caller", "+15555678", "Joey", "cut", tomorrow, "13:00")
    print(r)

    print(f"\n=== check_availability AGAIN — that slot should now be missing ===")
    r = await check_availability(tomorrow, stylist="Joey", service="cut")
    print(r)

asyncio.run(main())
