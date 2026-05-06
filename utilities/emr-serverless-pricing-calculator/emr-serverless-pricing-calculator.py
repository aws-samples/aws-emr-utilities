#!/usr/bin/env python3
"""EMR Serverless Pricing Calculator"""

# Pricing: {region: {arch: (vcpu, memory, storage)}}
PRICING = {
    "us-east-1":    {"graviton": (0.042094, 0.004628, 0.000111), "x86": (0.052624, 0.005786, 0.000111)},
    "us-east-2":    {"graviton": (0.042094, 0.004628, 0.000111), "x86": (0.052624, 0.005786, 0.000111)},
    "us-west-1":    {"graviton": (0.047494, 0.005222, 0.000111), "x86": (0.059374, 0.006530, 0.000111)},
    "us-west-2":    {"graviton": (0.042094, 0.004628, 0.000111), "x86": (0.052624, 0.005786, 0.000111)},
    "eu-west-1":    {"graviton": (0.046374, 0.005100, 0.000122), "x86": (0.057974, 0.006376, 0.000122)},
    "eu-central-1": {"graviton": (0.049574, 0.005452, 0.000128), "x86": (0.061974, 0.006816, 0.000128)},
    "ap-southeast-1": {"graviton": (0.047494, 0.005222, 0.000122), "x86": (0.059374, 0.006530, 0.000122)},
    "ap-northeast-1": {"graviton": (0.052774, 0.005804, 0.000128), "x86": (0.065974, 0.007256, 0.000128)},
}

def main():
    print("=" * 50)
    print("  EMR Serverless Pricing Calculator")
    print("=" * 50)

    vcpu_hours = float(input("\nEnter vCPU-hours: "))
    memory_hours = float(input("Enter memoryGB-hours: "))
    storage_hours = float(input("Enter storageGB-hours: "))

    print(f"\nAvailable regions: {', '.join(PRICING.keys())}")
    region = input("Enter region: ").strip()
    if region not in PRICING:
        print(f"Error: Region '{region}' not found. Available: {', '.join(PRICING.keys())}")
        return

    arch = input("Architecture (graviton/x86): ").strip().lower()
    if arch not in ("graviton", "x86"):
        print("Error: Choose 'graviton' or 'x86'.")
        return

    vcpu_price, mem_price, stor_price = PRICING[region][arch]

    vcpu_cost = round(vcpu_hours * vcpu_price, 2)
    mem_cost = round(memory_hours * mem_price, 2)
    stor_cost = round(storage_hours * stor_price, 2)
    total = round(vcpu_cost + mem_cost + stor_cost, 2)

    print(f"\n{'— Cost Breakdown —':^50}")
    print(f"  Region: {region} | Architecture: {arch}")
    print(f"  vCPU:    {vcpu_hours:>12.3f} hrs × ${vcpu_price:.6f} = ${vcpu_cost:>10.2f}")
    print(f"  Memory:  {memory_hours:>12.3f} hrs × ${mem_price:.6f} = ${mem_cost:>10.2f}")
    print(f"  Storage: {storage_hours:>12.3f} hrs × ${stor_price:.6f} = ${stor_cost:>10.2f}")
    print(f"  {'':->48}")
    print(f"  Total Estimated Cost: ${total:.2f}")

    discount_input = input("\nService discount %? (0 for none): ").strip()
    discount_pct = float(discount_input)

    if discount_pct > 0:
        discount_amt = round(total * discount_pct / 100, 2)
        final = round(total - discount_amt, 2)
        print(f"\n  Discount ({discount_pct}%): -${discount_amt:.2f}")
        print(f"  Final Cost After Discount: ${final:.2f}")
    else:
        print(f"\n  Final Cost: ${total:.2f}")

if __name__ == "__main__":
    main()
