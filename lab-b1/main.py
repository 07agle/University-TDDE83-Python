from logic import read_lines, parse_record, filter_records, summarize

def main():
    text_list = read_lines("registreringar.txt")
    student_list = []
    for line in text_list:
        student_list.append(parse_record(line))

    print(summarize(student_list))

    studens_in_course = filter_records(student_list, "kurskod", "TDDE82")
    if studens_in_course:
        print("\nStudenter på TDDE82:")
        for student in studens_in_course:
            student_first_name = student.get("fornamn")
            student_last_name = student.get("efternamn")
            print(f"   {student_first_name} {student_last_name}")


if __name__ == "__main__":
    main()
