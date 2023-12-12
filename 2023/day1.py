file_path = "./day1test.txt"
file_path = "./day1data.txt"

digits = [
    'zero','one','two','three','four','five','six','seven','eight','nine'
]

with open(file_path, "r") as file:
    sum = 0
    for line in file:
        first_digit = None
        last_digit = None
        i=0
        while i < len(line):
            ch = line[i]
            num = None
            if ch.isdigit():
                num = int(ch)
            else:
                for digit in digits:
                    if line[i:].startswith(digit):
                        num = digits.index(digit)
                        
            if num is not None:
                if first_digit is None:
                    first_digit = num
                last_digit = num
            i += 1
        checksum = first_digit * 10 + last_digit
        print(checksum)
        sum += checksum
    print(sum)
            

