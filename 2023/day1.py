file_path = "./day1test.txt"
file_path = "./day1data.txt"

with open(file_path, "r") as file:
    sum=0
    for line in file:
        first_digit = None
        last_digit = None
        for ch in line:
            if ch.isdigit():
                if first_digit is None:
                    first_digit = ch
                last_digit = ch
        sum += (ord(first_digit)-ord('0')) *10+ (ord(last_digit)-ord('0'))
    print(sum)

